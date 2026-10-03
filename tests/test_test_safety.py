"""P1 additions: independent-instance admission and static bypass checks."""
from pathlib import Path

import pytest

import conftest
from database_guard import (
    APPROVED_INSTANCE_DIRECTORY, APPROVED_TEST_DATABASE, DatabaseGuardError,
    forbidden_database_calls, migrate_test_store, open_test_store, unsafe_test_files,
)
from test_database_guard import FakeStore, fake_url_file


@pytest.mark.parametrize("directory", [
    str(APPROVED_INSTANCE_DIRECTORY.parent / "runtime_elsewhere" / "pg"),
    str(APPROVED_INSTANCE_DIRECTORY), str(APPROVED_INSTANCE_DIRECTORY) + "_other/data",
    str(APPROVED_INSTANCE_DIRECTORY / ".." / "workbench_runtime" / "pg"),
    "work/b1main_test_pg/data", "", 42,
])
def test_right_database_name_on_wrong_instance_is_rejected(fake_url_file, directory):
    store = FakeStore(APPROVED_TEST_DATABASE, directory=directory)
    with pytest.raises(DatabaseGuardError, match="independent instance directory"):
        open_test_store(fake_url_file, store_factory=lambda _: store)
    assert store.actions[-1] == "dispose"
    assert "migrate" not in store.actions


def test_guard_does_not_allow_environment_to_override_constants(fake_url_file, monkeypatch):
    monkeypatch.setenv("APPROVED_TEST_DATABASE", "workbench_meta")
    monkeypatch.setenv("APPROVED_INSTANCE_DIRECTORY", "/synthetic-other-instance")
    store = FakeStore("workbench_meta", directory="/synthetic-other-instance/data")
    with pytest.raises(DatabaseGuardError, match="approved workbench_test_b1main"):
        open_test_store(fake_url_file, store_factory=lambda _: store)
    assert "migrate" not in store.actions


def test_explicit_migration_proxy_rechecks_instance_before_ddl():
    store = FakeStore(APPROVED_TEST_DATABASE, directory="/synthetic-other-instance/data")
    with pytest.raises(DatabaseGuardError, match="independent instance directory"):
        migrate_test_store(store, from_version=1, to_version=2)
    assert "migrate" not in store.actions


def test_session_hook_without_connection_config_performs_no_admission(monkeypatch):
    monkeypatch.delenv("WORKBENCH_TEST_DB_URL_FILE", raising=False)
    monkeypatch.setattr(conftest, "open_test_store", lambda _: pytest.fail("Unexpected connection"))
    conftest.pytest_sessionstart(None)


def test_session_hook_admits_configured_database_without_migration(monkeypatch):
    store = FakeStore(APPROVED_TEST_DATABASE)
    monkeypatch.setenv("WORKBENCH_TEST_DB_URL_FILE", "synthetic-private-connection-path")
    requested = []
    monkeypatch.setattr(conftest, "open_test_store", lambda filename: requested.append(filename) or store)
    conftest.pytest_sessionstart(None)
    assert requested == ["synthetic-private-connection-path"]
    assert store.actions == ["dispose"]


def test_session_hook_rejects_failed_admission(monkeypatch):
    monkeypatch.setenv("WORKBENCH_TEST_DB_URL_FILE", "synthetic-private-connection-path")

    def reject(_):
        raise DatabaseGuardError("synthetic admission rejected")

    monkeypatch.setattr(conftest, "open_test_store", reject)
    with pytest.raises(pytest.UsageError, match="synthetic admission rejected"):
        conftest.pytest_sessionstart(None)


def test_no_direct_database_construction_or_migration_outside_guard():
    assert unsafe_test_files(Path(__file__).parent) == {}


@pytest.mark.parametrize("source", [
    "Sto" + "re(url)",
    "store.mig" + "rate()",
    "create_" + "engine(url)",
    "from analysis_agent.storage import Store as Alias\nAlias(url)",
    "from sqlalchemy import create_engine as engine_factory\nengine_factory(url)",
    "factory = Store\nfactory(url)",
    'script = "Sto' + 're(url)"',
    'script = "store.mig' + 'rate()"',
])
def test_static_check_detects_calls_aliases_and_embedded_scripts(source):
    assert forbidden_database_calls(source)


def test_static_check_does_not_confuse_fake_store_with_real_store():
    assert forbidden_database_calls("store = FakeStore('synthetic')") == []
