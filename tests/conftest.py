"""Session-wide admission, before collection and before any fixture may run."""
import os

import pytest

from database_guard import DatabaseGuardError, open_test_store


def pytest_sessionstart(session):
    filename = os.environ.get("WORKBENCH_TEST_DB_URL_FILE")
    if not filename:
        return  # Pure tests remain available; PostgreSQL fixtures explicitly skip.
    try:
        store = open_test_store(filename)
    except DatabaseGuardError as exc:
        raise pytest.UsageError(str(exc)) from None
    store.engine.dispose()  # No automatic version check or schema migration.
