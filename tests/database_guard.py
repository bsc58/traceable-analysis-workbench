"""The only test entry point for a real database, always fail closed.

The permitted database and instance directory are fixed, not environment overrides.
Constructing Store creates an engine but performs no SQL. Admission reads identity
before any version check, migration, fixture write, or use by a Workbench.
"""
import ast
from pathlib import Path
import re

from sqlalchemy import text

from analysis_agent.storage import Store


from types import MappingProxyType
WORKSPACE_ROOT = Path(__file__).resolve().parents[3]
APPROVED_TEST_INSTANCES = MappingProxyType({
    "workbench_test_b1main": WORKSPACE_ROOT / "work" / "b1main_test_pg",
    "workbench_test_b1p7": WORKSPACE_ROOT / "work" / "b1p7_test_pg",
    "workbench_test_b1p9a": WORKSPACE_ROOT / "work" / "b1p9a_test_pg",
    "workbench_test_b2e": WORKSPACE_ROOT / "work" / "b2e_test_pg",
    "workbench_test_b2r": WORKSPACE_ROOT / "work" / "b2r_test_pg",
})
# Compatibility aliases used by pure guard tests; admission always checks the pair.
APPROVED_TEST_DATABASE = "workbench_test_b1main"
APPROVED_INSTANCE_DIRECTORY = APPROVED_TEST_INSTANCES[APPROVED_TEST_DATABASE]


class DatabaseGuardError(RuntimeError):
    """A safe, connection-detail-free failure of test database admission."""


def _dispose(store):
    try:
        store.engine.dispose()
    except Exception:
        pass  # A cleanup failure must not replace the safe admission error.


def verify_test_store(store):
    """Read both server identities; never admit a name on another instance."""
    try:
        with store.engine.connect() as connection:
            database = connection.execute(text("SELECT current_database()")).scalar_one()
            if database not in APPROVED_TEST_INSTANCES:
                raise DatabaseGuardError("Tests require the approved workbench_test_b1main / P7 / P9a / B2E / B2R database and instance pair; no migration was attempted")
            directory = connection.execute(text("SELECT current_setting('data_directory')")).scalar_one()
            approved = APPROVED_TEST_INSTANCES[database]
            valid_directory = (
                isinstance(directory, str) and bool(directory)
                and Path(directory).is_absolute()
                and approved.resolve() == approved
                and approved in Path(directory).resolve().parents
            )
            if not valid_directory:
                raise DatabaseGuardError("Tests require the approved independent instance directory; no migration was attempted")
    except DatabaseGuardError:
        _dispose(store)
        raise
    except Exception:
        _dispose(store)
        raise DatabaseGuardError("Test database identity check failed; connection details suppressed") from None
    store._approved_test_database = database
    return store


def _open_url(url, *, store_factory=Store):
    try:
        store = store_factory(url)
    except Exception:
        raise DatabaseGuardError("Test database initialization failed; connection details suppressed") from None
    return verify_test_store(store)


def open_test_store(filename, *, store_factory=Store):
    """Load connection config internally and admit a Store without migration."""
    try:
        url = Path(filename).read_text(encoding="utf-8").strip()
    except Exception:
        raise DatabaseGuardError("Test database initialization failed; connection details suppressed") from None
    return _open_url(url, store_factory=store_factory)


def reopen_test_store(store):
    """Create a new connection pool and independently recheck server identity."""
    return _open_url(store.engine.url)


def migrate_test_store(store, *, from_version, to_version):
    """Explicit migration-only proxy: re-admit immediately before every DDL call."""
    verify_test_store(store)
    try:
        return store.migrate(expected_database=store._approved_test_database,
                             from_version=from_version, to_version=to_version)
    except Exception:
        _dispose(store)
        raise DatabaseGuardError("Test database migration failed; connection details suppressed") from None


_FORBIDDEN_CALL = re.compile(r"(?<![\w])(?:Store|create_engine)\s*\(|\.\s*migrate\s*\(")


def forbidden_database_calls(source):
    """Find direct calls, aliases, and embedded snippets without matching FakeStore.

    The text check covers snippets in string constants and comments; the AST check
    additionally covers aliases imported or assigned in executable Python.
    """
    violations = [(source.count("\n", 0, match.start()) + 1, "forbidden database call")
                  for match in _FORBIDDEN_CALL.finditer(source)]
    tree = ast.parse(source)
    aliases = {"Store", "create_engine"}
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            for name in node.names:
                if name.name in {"Store", "create_engine"}:
                    aliases.add(name.asname or name.name)
    changed = True
    while changed:
        changed = False
        for node in ast.walk(tree):
            if isinstance(node, (ast.Assign, ast.AnnAssign)):
                value = node.value
                name = value.id if isinstance(value, ast.Name) else None
                if name not in aliases:
                    continue
                targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                for target in targets:
                    if isinstance(target, ast.Name) and target.id not in aliases:
                        aliases.add(target.id)
                        changed = True
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        callee = node.func
        if ((isinstance(callee, ast.Name) and callee.id in aliases)
                or (isinstance(callee, ast.Attribute)
                    and callee.attr in {"Store", "create_engine", "migrate"})):
            violations.append((node.lineno, "forbidden database call or alias"))
    return sorted(set(violations))


def unsafe_test_files(tests_directory):
    """Only this module may construct stores/engines or call migration directly."""
    findings = {}
    for path in Path(tests_directory).rglob("*.py"):
        if path.resolve() == Path(__file__).resolve():
            continue
        violations = forbidden_database_calls(path.read_text(encoding="utf-8"))
        if violations:
            findings[str(path)] = violations
    return findings


class FakeMigrationProbe:
    """Run Store schema-boundary logic against an in-memory SELECT-only double.

    No engine/driver constructor or external connection is used. Unexpected SQL
    fails immediately. Tests that exercise successful DDL dispatch must replace
    the migration function with an in-memory spy first.
    """

    def __init__(self, *, database="synthetic_metadata", version=2, tables=None, version_rows=None):
        from contextlib import nullcontext
        from types import SimpleNamespace

        self.database = database
        self.version_rows = [version] if version_rows is None else list(version_rows)
        base_tables = {"aw_runs", "aw_events", "aw_steps", "aw_evidence", "aw_releases", "aw_schema_versions"}
        if version == 0:
            base_tables = set()
        elif version >= 2:
            base_tables |= {"aw_attempts", "aw_resolutions", "aw_migration_journal"}
        if version >= 3:
            base_tables.add("aw_revalidations")
        if version >= 4:
            base_tables.add("aw_access_refusals")
        if version >= 5:
            base_tables |= {"aw_model_invocations", "aw_budget_accounts", "aw_run_execution"}
        if version >= 6:
            base_tables |= {"aw_dataset_versions", "aw_snapshot_sets"}
        if version >= 7:
            base_tables |= {"aw_daily_sessions", "aw_decisions", "aw_decision_reviews"}
        self.tables = base_tables if tables is None else set(tables)
        self.statements = []
        self._store = Store.__new__(Store)
        self._store.engine = SimpleNamespace(connect=lambda: nullcontext(self), begin=lambda: nullcontext(self))

    def execute(self, statement):
        sql = str(statement)
        self.statements.append(sql)
        if sql == "SELECT current_database()":
            value = [self.database]
        elif sql.startswith("SELECT pg_advisory_xact_lock("):
            value = [None]
        elif sql.startswith("SELECT tablename FROM pg_catalog.pg_tables"):
            value = sorted(self.tables)
        elif sql == "SELECT to_regclass('public.aw_schema_versions')":
            value = ["aw_schema_versions" if "aw_schema_versions" in self.tables else None]
        elif sql == "SELECT aw_schema_versions.version \nFROM aw_schema_versions":
            value = list(self.version_rows)
        else:
            raise AssertionError("Pure fake schema probe rejects unexpected SQL")
        return _FakeSchemaResult(value)

    def invoke(self, operation, **kwargs):
        if operation not in {"read_version", "check_version", "migrate", "rollback"}:
            raise AssertionError("Unsupported fake schema probe operation")
        return getattr(self._store, operation)(**kwargs)


class _FakeSchemaResult:
    def __init__(self, values):
        self.values = values

    def scalar_one(self):
        if len(self.values) != 1:
            raise AssertionError("Fake scalar query did not return exactly one value")
        return self.values[0]

    def scalars(self):
        return self

    def all(self):
        return list(self.values)

    def __iter__(self):
        return iter(self.values)


def rollback_test_store(store, *, from_version, to_version):
    verify_test_store(store)
    return store.rollback(expected_database=store._approved_test_database,
                          from_version=from_version, to_version=to_version)
