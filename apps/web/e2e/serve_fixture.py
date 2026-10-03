"""Opt-in local E2E server on the approved P9a instance; stop via SIGINT."""
import json
import os
from pathlib import Path
import secrets
import shutil
import signal
import sys

ROOT = Path(__file__).resolve().parents[3]
WORKSPACE = ROOT.parent.parent
INSTANCE = WORKSPACE / "work" / "b1p9a_test_pg"
sys.path[:0] = [str(ROOT / "src"), str(ROOT / "tests"), str(ROOT / "scripts")]
import local_postgres
from database_guard import open_test_store, migrate_test_store
from analysis_agent.storage import SCHEMA_VERSION
from fixture_support import make_workbench, seed, app_for
import uvicorn


def main():
    os.umask(0o077)
    local_postgres.DATABASE = "workbench_test_b1p9a"
    server = local_postgres.LocalPostgres(INSTANCE / "pg")
    store = None
    try:
        with server.lock():
            server.start()  # Do not print connection configuration.
        store = open_test_store(server.url_file)
        if store.read_version() == 0:
            migrate_test_store(store, from_version=0, to_version=SCHEMA_VERSION)
        else:
            store.check_version()
        wb = make_workbench(store, INSTANCE / "ui-objects")
        ids = seed(wb)
        token_file = INSTANCE / "ui.token"
        if not token_file.exists():
            token_file.write_text(secrets.token_urlsafe(48))
            token_file.chmod(0o600)
        token = token_file.read_text().strip()
        evidence = ROOT / "docs" / "ui" / "evidence"
        evidence.mkdir(parents=True, exist_ok=True)
        (evidence / "fixture-ids.json").write_text(json.dumps(ids, indent=2))
        print("P9a fixture API ready; only public synthetic records", flush=True)
        uvicorn.run(app_for(wb, token), host="127.0.0.1", port=8911, log_level="warning")
    finally:
        if store:
            store.engine.dispose()
        with server.lock():
            server.stop()
        if INSTANCE.exists():
            shutil.rmtree(INSTANCE)
        if server.socket.exists():
            shutil.rmtree(server.socket)
        print("P9a test instance and socket removed", flush=True)

if __name__ == "__main__":
    main()
