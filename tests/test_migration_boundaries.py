"""Pure fake connections and source ASTs: explicit migration admission boundaries."""
import ast
from pathlib import Path

import pytest

from analysis_agent.contracts import WorkbenchError
from analysis_agent import migrations
from database_guard import FakeMigrationProbe


PUBLIC_ROOT = Path(__file__).resolve().parents[1]
CLI_FILE = PUBLIC_ROOT / "src" / "analysis_agent" / "cli.py"
ROLLBACK_FILE = PUBLIC_ROOT / "scripts" / "rollback_metadata.py"


def source_tree(path):
    return ast.parse(path.read_text(encoding="utf-8"))


def function_node(tree, name):
    return next(node for node in ast.walk(tree)
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name)


def attribute_calls(node, name):
    return [child for child in ast.walk(node) if isinstance(child, ast.Call)
            and isinstance(child.func, ast.Attribute) and child.func.attr == name]


def assert_boundary_error(probe, code, operation, **kwargs):
    with pytest.raises(WorkbenchError) as caught:
        probe.invoke(operation, **kwargs)
    assert caught.value.code == code
    assert all(statement.startswith("SELECT ") for statement in probe.statements)


@pytest.mark.parametrize("path,name", [
    (CLI_FILE, "demo_workbench"),
])
def test_both_factories_check_version_and_never_dispatch_migration(path, name):
    factory = function_node(source_tree(path), name)
    checks = attribute_calls(factory, "check_version")
    assert len(checks) == 1
    assert attribute_calls(factory, "migrate") == []
    constructors = [node for node in ast.walk(factory) if isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Name) and node.func.id == "Workbench"]
    assert len(constructors) == 1 and checks[0].lineno < constructors[0].lineno


def test_only_explicit_cli_migration_command_calls_migration_in_application_sources():
    found = []

    class EntryCalls(ast.NodeVisitor):
        def __init__(self, filename):
            self.filename = filename
            self.functions = []

        def visit_FunctionDef(self, node):
            self.functions.append(node.name)
            self.generic_visit(node)
            self.functions.pop()

        def visit_Call(self, node):
            if isinstance(node.func, ast.Attribute) and node.func.attr == "migrate":
                found.append((self.filename, tuple(self.functions)))
            self.generic_visit(node)

    paths = list((PUBLIC_ROOT / "src" / "analysis_agent").glob("*.py"))
    for path in paths:
        EntryCalls(path).visit(source_tree(path))
    assert found == [(CLI_FILE, ("migration_command",))]


def test_cli_migration_branch_precedes_factory_and_returns_before_runtime_startup():
    main = function_node(source_tree(CLI_FILE), "main")
    migration_calls = [node for node in ast.walk(main) if isinstance(node, ast.Call)
                       and isinstance(node.func, ast.Name) and node.func.id == "migration_command"]
    assert len(migration_calls) == 1
    dispatch = next(node for node in ast.walk(main) if isinstance(node, ast.If)
                    and migration_calls[0] in list(ast.walk(node)))
    assert ast.unparse(dispatch.test) == "args.command == 'migrate'"
    assert isinstance(dispatch.body[-1], ast.Return)
    factory_call = next(node for node in ast.walk(main) if isinstance(node, ast.Call)
                        and isinstance(node.func, ast.Name) and node.func.id == "factory")
    assert dispatch.end_lineno < factory_call.lineno


@pytest.mark.parametrize("path,method", [(CLI_FILE, "migrate"), (ROLLBACK_FILE, "rollback")])
def test_explicit_migration_and_rollback_require_and_forward_expected_database(path, method):
    tree = source_tree(path)
    expected_arguments = [node for node in attribute_calls(tree, "add_argument")
                          if node.args and isinstance(node.args[0], ast.Constant)
                          and node.args[0].value == "--expected-database"]
    assert len(expected_arguments) == 1
    assert any(keyword.arg == "required" and isinstance(keyword.value, ast.Constant)
               and keyword.value.value is True for keyword in expected_arguments[0].keywords)
    calls = attribute_calls(tree, method)
    assert len(calls) == 1
    forwarded = next(keyword.value for keyword in calls[0].keywords if keyword.arg == "expected_database")
    assert ast.unparse(forwarded) == "args.expected_database"


def test_check_version_accepts_v2_using_only_read_queries():
    probe = FakeMigrationProbe(version=2)
    assert probe.invoke("check_version", expected_version=2) == 2
    assert all(statement.startswith("SELECT ") for statement in probe.statements)
    assert not any("advisory" in statement for statement in probe.statements)


@pytest.mark.parametrize("version", [0, 1])
def test_check_version_rejects_older_versions_without_migration(version):
    probe = FakeMigrationProbe(version=version)
    assert_boundary_error(probe, "migration_required", "check_version")
    assert not any("advisory" in statement for statement in probe.statements)


@pytest.mark.parametrize("rows", [[99], [-1], [], [1, 2]])
def test_unknown_or_ambiguous_schema_versions_are_rejected(rows):
    probe = FakeMigrationProbe(version_rows=rows)
    assert_boundary_error(probe, "unsupported_schema", "check_version")


@pytest.mark.parametrize("tables", [
    {"aw_runs"}, {"aw_schema_versions"},
    {"aw_runs", "aw_events", "aw_steps", "aw_evidence", "aw_releases", "aw_schema_versions"},
])
def test_incomplete_schema_is_not_silently_repaired(tables):
    probe = FakeMigrationProbe(version=2, tables=tables)
    assert_boundary_error(probe, "unsupported_schema", "check_version")


@pytest.mark.parametrize("expected", ["", "another_database", "workbench_meta"])
def test_migration_rejects_mismatched_database_before_lock_or_schema_inspection(expected):
    probe = FakeMigrationProbe()
    assert_boundary_error(probe, "database_mismatch", "migrate",
                          expected_database=expected, from_version=1, to_version=2)
    assert probe.statements == ["SELECT current_database()"]


def test_migration_requires_exact_source_version_even_after_prior_upgrade():
    probe = FakeMigrationProbe(version=2)
    assert_boundary_error(probe, "migration_version_mismatch", "migrate",
                          expected_database="synthetic_metadata", from_version=1, to_version=2)


@pytest.mark.parametrize("from_version,to_version", [(0, 99), (1, 0), (2, 1)])
def test_migration_refuses_unsupported_direction_or_target(from_version, to_version):
    probe = FakeMigrationProbe(version=from_version)
    assert_boundary_error(probe, "unsupported_schema", "migrate",
                          expected_database="synthetic_metadata", from_version=from_version,
                          to_version=to_version)


def test_unknown_actual_schema_is_rejected_by_migration_before_upgrade_dispatch(monkeypatch):
    monkeypatch.setattr(migrations, "upgrade", lambda *args: pytest.fail("Unexpected upgrade"))
    probe = FakeMigrationProbe(version_rows=[99])
    assert_boundary_error(probe, "unsupported_schema", "migrate",
                          expected_database="synthetic_metadata", from_version=99, to_version=2)


def test_migration_dispatches_sequential_version_steps_to_pure_spy(monkeypatch):
    dispatched = []
    monkeypatch.setattr(migrations, "upgrade", lambda connection, before, after: dispatched.append((before, after)))
    probe = FakeMigrationProbe(version=0)
    result = probe.invoke("migrate", expected_database="synthetic_metadata", from_version=0, to_version=2)
    assert dispatched == [(0, 1), (1, 2)]
    assert result == {"metadata_schema": 2, "from_version": 0, "to_version": 2,
                      "changed": True, "source_mutations": 0}


def test_explicit_same_version_migration_is_idempotent_without_upgrade_dispatch(monkeypatch):
    monkeypatch.setattr(migrations, "upgrade", lambda *args: pytest.fail("Unexpected upgrade"))
    probe = FakeMigrationProbe(version=2)
    result = probe.invoke("migrate", expected_database="synthetic_metadata", from_version=2, to_version=2)
    assert result["changed"] is False
    assert result["metadata_schema"] == 2


@pytest.mark.parametrize("expected", ["", "another_database", "workbench_meta"])
def test_rollback_requires_matching_database_before_any_schema_inspection(expected):
    probe = FakeMigrationProbe()
    assert_boundary_error(probe, "database_mismatch", "rollback", expected_database=expected)
    assert probe.statements == ["SELECT current_database()"]


def test_rollback_requires_exact_source_version():
    probe = FakeMigrationProbe(version=1)
    assert_boundary_error(probe, "migration_version_mismatch", "rollback",
                          expected_database="synthetic_metadata")


def test_rollback_refuses_unknown_direction():
    probe = FakeMigrationProbe(version=2)
    assert_boundary_error(probe, "unsupported_schema", "rollback",
                          expected_database="synthetic_metadata", from_version=2, to_version=0)


def test_rollback_dispatches_only_requested_v2_to_v1_to_pure_spy(monkeypatch):
    dispatched = []
    monkeypatch.setattr(migrations, "downgrade", lambda connection: dispatched.append("downgrade"))
    probe = FakeMigrationProbe(version=2)
    result = probe.invoke("rollback", expected_database="synthetic_metadata", from_version=2, to_version=1)
    assert dispatched == ["downgrade"]
    assert result["metadata_schema"] == 1
    assert result["legacy_records_unchanged"] is True


def test_business_factory_check_excluded_from_public_suite():
    pytest.skip("Separate business package is outside this public-only batch; no file access")
