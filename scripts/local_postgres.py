#!/usr/bin/env python3
"""Manage an isolated PostgreSQL instance for local workbench development.

Requires pgserver==0.1.4 in this Python environment. PostgreSQL binaries are
provided by that package; this helper needs no running system PostgreSQL,
Docker, root account, passwords, or application imports. The resulting URL
uses the SQLAlchemy psycopg driver (install psycopg separately for the app).

This is a single-user development helper, not a production deployment tool.
The server stays running after `start` exits; `stop` preserves all data.
"""

from __future__ import annotations

import argparse
import contextlib
import fcntl
import hashlib
import json
import os
from pathlib import Path
import pwd
import stat
import subprocess
import sys
import tempfile
from urllib.parse import quote


PROJECT_ROOT = Path(__file__).resolve().parents[1]
WORK_ROOT = PROJECT_ROOT.parent.parent if PROJECT_ROOT.parent.name == "outputs" else PROJECT_ROOT.parent
DEFAULT_STATE_DIR = WORK_ROOT / "work" / "workbench_runtime" / "pg"
DATABASE = "workbench_meta"
PORT = 5432  # Unix socket filename suffix only; no TCP listener is created.
MARKER = "workbench-local-postgres.json"


def secure_directory(path: Path) -> None:
    if path.is_symlink():
        raise RuntimeError(f"Refusing symlink directory: {path}")
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    info = path.stat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid():
        raise RuntimeError(f"Directory is not owned by the current OS user: {path}")
    path.chmod(0o700)


def write_private(path: Path, content: str) -> None:
    """Atomically publish a private file without following a destination symlink."""
    if path.is_symlink():
        raise RuntimeError(f"Refusing symlink file: {path}")
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def sql_identifier(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'


def sql_string(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


class LocalPostgres:
    def __init__(self, state_dir: Path):
        if os.getuid() == 0:
            raise RuntimeError("Run this development helper as your ordinary OS user, not root")
        if state_dir.is_symlink():
            raise RuntimeError("The state directory must not be a symlink")
        self.state = state_dir.expanduser().resolve()
        if self.state == PROJECT_ROOT or PROJECT_ROOT in self.state.parents:
            raise RuntimeError("Keep PostgreSQL state outside the public project directory")
        self.user = pwd.getpwuid(os.getuid()).pw_name
        digest = hashlib.sha256(str(self.state).encode()).hexdigest()[:16]
        self.socket = Path("/tmp") / f"aaw-pg-{os.getuid()}-{digest}"
        self.url_file = self.state.parent / "metadata.url"
        self.log_file = self.state.parent / "postgres.log"
        self.lock_file = self.state.parent / f".postgres-{digest}.lock"
        try:
            import pgserver
        except ImportError as exc:
            raise RuntimeError("Use a Python environment with pgserver==0.1.4 installed") from exc
        # pgserver 0.1.4 documents this path but doesn't re-export the constant.
        self.bin = Path(pgserver.__file__).resolve().parent / "pginstall" / "bin"
        if not (self.bin / "pg_ctl").is_file():
            raise RuntimeError("pgserver's bundled PostgreSQL binaries are unavailable on this platform")
        self.env = {key: value for key, value in os.environ.items() if not key.startswith("PG")}

    @contextlib.contextmanager
    def lock(self):
        secure_directory(self.state.parent)
        flags = os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(self.lock_file, flags, 0o600)
        try:
            os.fchmod(descriptor, 0o600)
            fcntl.flock(descriptor, fcntl.LOCK_EX)
            yield
        finally:
            os.close(descriptor)

    def command(self, name: str, *args: str, check: bool = True) -> subprocess.CompletedProcess:
        # Temporary files keep pg_ctl descendants from inheriting a captured pipe.
        with tempfile.TemporaryFile(mode="w+") as stdout, tempfile.TemporaryFile(mode="w+") as stderr:
            completed = subprocess.run(
                [str(self.bin / name), *map(str, args)], stdout=stdout, stderr=stderr,
                text=True, env=self.env, timeout=45, start_new_session=True,
            )
            stdout.seek(0)
            stderr.seek(0)
            result = subprocess.CompletedProcess(completed.args, completed.returncode, stdout.read(), stderr.read())
        if check and result.returncode:
            raise RuntimeError(f"{name} failed ({result.returncode}): {result.stderr.strip() or result.stdout.strip()}")
        return result

    def managed(self) -> bool:
        marker = self.state / MARKER
        if not marker.is_file():
            if (self.state / "PG_VERSION").exists():
                raise RuntimeError("Refusing an existing PostgreSQL cluster without this helper's ownership marker")
            return False
        saved = json.loads(marker.read_text())
        if saved != {"format": 1, "owner_uid": os.getuid(), "owner_name": self.user, "purpose": "local-workbench-development"}:
            raise RuntimeError("This PostgreSQL cluster belongs to a different owner or helper format")
        if self.state.stat().st_uid != os.getuid():
            raise RuntimeError("The PostgreSQL data directory is not owned by the current OS user")
        return True

    def running(self) -> bool:
        if not self.managed() or not (self.state / "PG_VERSION").exists():
            return False
        status = self.command("pg_ctl", "-D", self.state, "status", check=False)
        if status.returncode not in (0, 3):
            raise RuntimeError(f"Unable to determine PostgreSQL status: {status.stderr.strip()}")
        if status.returncode == 3 and (self.state / "postmaster.pid").exists():
            # A restricted shell can make pg_ctl's kill(pid, 0) look like a
            # stopped server. Never try to start a second process in that case.
            try:
                pid = int((self.state / "postmaster.pid").read_text().splitlines()[0])
                os.kill(pid, 0)
            except PermissionError as exc:
                raise RuntimeError("The current shell cannot inspect the PostgreSQL process; retry outside its sandbox") from exc
            except ProcessLookupError:
                pass
            else:
                raise RuntimeError("PostgreSQL has a live PID but pg_ctl cannot verify it; inspect the process before restarting")
        return status.returncode == 0

    def psql(self, sql: str, database: str = "postgres") -> str:
        return self.command(
            "psql", "-X", "-v", "ON_ERROR_STOP=1", "-h", self.socket,
            "-p", str(PORT), "-U", self.user, "-d", database, "-A", "-t", "-c", sql,
        ).stdout.strip()

    def url(self) -> str:
        return f"postgresql+psycopg://{quote(self.user, safe='')}@/{DATABASE}?host={quote(str(self.socket), safe='')}&port={PORT}"

    def start(self) -> dict:
        initialized = self.managed()
        if not initialized:
            secure_directory(self.state)
            if any(self.state.iterdir()):
                raise RuntimeError("Refusing to initialize a nonempty, unmanaged state directory")
            self.command("initdb", "-D", self.state, "--username", self.user,
                         "--auth-local=peer", "--auth-host=reject", "--encoding=UTF8", "--locale=C")
            write_private(self.state / MARKER, json.dumps({
                "format": 1, "owner_uid": os.getuid(), "owner_name": self.user,
                "purpose": "local-workbench-development",
            }) + "\n")
        secure_directory(self.state)
        secure_directory(self.socket)
        if not self.running():
            # HBA also rejects every other DB username and all IP connections.
            if not (self.state / ".workbench-role-auth").exists():
                write_private(self.state / "pg_hba.conf", (
                "# Local development: one OS identity through a private Unix socket.\n"
                f"local all {sql_identifier(self.user)} peer\n"
                "local all all reject\n"
                "local replication all reject\n"
                "host all all 0.0.0.0/0 reject\n"
                "host all all ::0/0 reject\n"
            ))
            write_private(self.state / "workbench.conf", (
                "listen_addresses = ''\n"
                f"unix_socket_directories = {sql_string(str(self.socket))}\n"
                "unix_socket_permissions = 0700\n"
                f"port = {PORT}\n"
                "max_connections = 40\n"
                "shared_buffers = '64MB'\n"
                "timezone = 'UTC'\n"
                "log_timezone = 'UTC'\n"
                "fsync = on\n"
                "synchronous_commit = on\n"
                "logging_collector = off\n"
            ))
            config = self.state / "postgresql.conf"
            include = "include = 'workbench.conf'"
            existing = config.read_text()
            if include not in existing:
                write_private(config, existing + "\n" + include + "\n")
            if not self.log_file.exists():
                write_private(self.log_file, "")
            self.log_file.chmod(0o600)
            self.command("pg_ctl", "-D", self.state, "-l", self.log_file, "-w", "-t", "30", "start")
        for database in dict.fromkeys((self.user, DATABASE)):
            present = self.psql(f"SELECT 1 FROM pg_database WHERE datname = {sql_string(database)}")
            if present != "1":
                self.psql(f"CREATE DATABASE {sql_identifier(database)} OWNER {sql_identifier(self.user)}")
            self.psql(f"REVOKE ALL ON DATABASE {sql_identifier(database)} FROM PUBLIC")
            self.psql("REVOKE ALL ON SCHEMA public FROM PUBLIC", database)
            self.psql(f"GRANT ALL ON SCHEMA public TO {sql_identifier(self.user)}", database)
        result = self.status()
        if result["status"] != "running" or result["listen_addresses"] != "":
            raise RuntimeError("PostgreSQL did not satisfy the Unix-socket-only readiness check")
        write_private(self.url_file, self.url() + "\n")
        result["url_file"] = str(self.url_file)
        return result

    def status(self) -> dict:
        if not self.running():
            return {"status": "stopped" if self.managed() else "not_initialized", "development_only": True}
        row = self.psql("""SELECT json_build_object(
            'status', 'running', 'server_version', current_setting('server_version'),
            'database', current_database(), 'user', current_user,
            'listen_addresses', current_setting('listen_addresses'),
            'unix_socket_directories', current_setting('unix_socket_directories'),
            'unix_socket_permissions', current_setting('unix_socket_permissions'),
            'development_only', true)
        """, DATABASE)
        return json.loads(row)

    def stop(self) -> dict:
        if self.running():
            self.command("pg_ctl", "-D", self.state, "-m", "fast", "-w", "-t", "30", "stop")
        return {"status": "stopped", "data_preserved": True, "development_only": True}

    def connection_info(self) -> dict:
        result = self.status()
        if result["status"] != "running":
            raise RuntimeError("Start the local database before requesting connection information")
        result.update({"sqlalchemy_url": self.url(), "url_file": str(self.url_file)})
        return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("start", "status", "stop", "connection-info"))
    parser.add_argument("--state-dir", type=Path, default=DEFAULT_STATE_DIR,
                        help="Private PostgreSQL data directory; default is outside the public project")
    args = parser.parse_args(argv)
    os.umask(0o077)
    try:
        server = LocalPostgres(args.state_dir)
        with server.lock():
            result = getattr(server, args.command.replace("-", "_"))()
        print(json.dumps(result, indent=2))
        return 0
    except (RuntimeError, OSError, subprocess.SubprocessError, ValueError) as exc:
        print(f"local_postgres: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
