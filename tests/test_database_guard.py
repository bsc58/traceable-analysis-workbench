"""The original P0 group of 16 cases, using synthetic connections only."""
from contextlib import contextmanager
from types import SimpleNamespace
import traceback

import pytest

from database_guard import (
    APPROVED_INSTANCE_DIRECTORY, DatabaseGuardError, migrate_test_store, open_test_store,
)


class FakeStore:
    def __init__(self, database, *, directory=None, fail_at=None):
        self.database = database
        self.directory = str(APPROVED_INSTANCE_DIRECTORY / "data") if directory is None else directory
        self.fail_at = fail_at
        self.actions = []
        self.engine = SimpleNamespace(connect=self.connect, dispose=self.dispose)

    @contextmanager
    def connect(self):
        self.actions.append("connect")
        if self.fail_at == "connect":
            raise RuntimeError("synthetic-connection-secret")
        yield self

    def execute(self, statement):
        query = str(statement)
        assert query in {"SELECT current_database()", "SELECT current_setting('data_directory')"}
        self.last_query = query
        self.actions.append("identity_query" if query.endswith("current_database()") else "directory_query")
        if self.fail_at == "query":
            raise RuntimeError("synthetic-connection-secret")
        return self

    def scalar_one(self):
        database_query = self.last_query.endswith("current_database()")
        self.actions.append("identity_result" if database_query else "directory_result")
        if self.fail_at == "result":
            raise RuntimeError("synthetic-connection-secret")
        return self.database if database_query else self.directory

    def migrate(self, **kwargs):
        assert kwargs == {"expected_database": "workbench_test_b1main", "from_version": 1, "to_version": 2}
        self.actions.append("migrate")
        if self.fail_at == "migrate":
            raise RuntimeError("synthetic-connection-secret")

    def dispose(self):
        self.actions.append("dispose")


@pytest.fixture
def fake_url_file(tmp_path):
    path = tmp_path / "synthetic-test-connection.txt"
    path.write_text("synthetic-connection-secret", encoding="utf-8")
    return path


@pytest.mark.parametrize("database", [
    "workbench_meta", "restricted_metadata", "private_production_db", "another_database",
    "workbench_test_b1main_extra", "WORKBENCH_TEST_P1", " workbench_test_b1main", "", None,
])
def test_guard_rejects_every_unapproved_database_before_migration(fake_url_file, database):
    store = FakeStore(database)
    with pytest.raises(DatabaseGuardError, match="approved workbench_test_b1main"):
        open_test_store(fake_url_file, store_factory=lambda _: store)
    assert store.actions == ["connect", "identity_query", "identity_result", "dispose"]


def test_guard_allows_only_verified_test_database_without_migration(fake_url_file):
    store = FakeStore("workbench_test_b1main")
    assert open_test_store(fake_url_file, store_factory=lambda _: store) is store
    assert store.actions == ["connect", "identity_query", "identity_result", "directory_query", "directory_result"]


@pytest.mark.parametrize("failure", ["connect", "query", "result"])
def test_guard_identity_errors_are_safe_and_never_migrate(fake_url_file, failure):
    store = FakeStore("workbench_test_b1main", fail_at=failure)
    with pytest.raises(DatabaseGuardError, match="identity check failed") as caught:
        open_test_store(fake_url_file, store_factory=lambda _: store)
    assert "migrate" not in store.actions
    assert store.actions[-1] == "dispose"
    assert "synthetic-connection-secret" not in "".join(traceback.format_exception(caught.value))


def test_guard_initialization_error_does_not_leak_connection_details(fake_url_file):
    def failed_factory(_):
        raise RuntimeError("synthetic-connection-secret")

    with pytest.raises(DatabaseGuardError, match="initialization failed") as caught:
        open_test_store(fake_url_file, store_factory=failed_factory)
    assert "synthetic-connection-secret" not in "".join(traceback.format_exception(caught.value))


def test_guard_missing_config_never_constructs_store(tmp_path):
    constructed = []
    with pytest.raises(DatabaseGuardError, match="initialization failed"):
        open_test_store(tmp_path / "absent", store_factory=lambda _: constructed.append(True))
    assert constructed == []


def test_guard_explicit_migration_error_is_safe_after_readmission(fake_url_file):
    store = FakeStore("workbench_test_b1main", fail_at="migrate")
    with pytest.raises(DatabaseGuardError, match="migration failed") as caught:
        migrate_test_store(store, from_version=1, to_version=2)
    assert store.actions == ["connect", "identity_query", "identity_result", "directory_query", "directory_result", "migrate", "dispose"]
    assert "synthetic-connection-secret" not in "".join(traceback.format_exception(caught.value))
