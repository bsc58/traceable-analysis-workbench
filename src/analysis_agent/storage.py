"""PostgreSQL metadata and durable, content-addressed local JSON objects."""
from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from pathlib import Path

from sqlalchemy import (CheckConstraint, Column, ForeignKeyConstraint, Index, Integer, Float,
                        MetaData, String, Table, UniqueConstraint,
                        create_engine, insert, select, text, update)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.engine import URL, make_url

from .contracts import WorkbenchError, canonical, now
from .path_policy import runtime_path

SCHEMA_VERSION = 7
RUN_STATES = "'ADMITTED','RUNNING','WAITING_RECONCILIATION','SUCCEEDED','FAILED','CANCELLED'"
ATTEMPT_STATES = "'DISPATCHED','ACCEPTED','FAILED','REJECTED_AFTER_EXECUTION','UNKNOWN','RESOLVED_FAILED'"

metadata = MetaData()
runs = Table("aw_runs", metadata,
    Column("id", String, primary_key=True), Column("project_id", String, nullable=False),
    Column("actor", String, nullable=False), Column("idempotency_key", String, nullable=False),
    Column("request_digest", String, nullable=False), Column("manifest", JSONB, nullable=False),
    Column("state", String, nullable=False), Column("seq", Integer, nullable=False, default=0),
    Column("tool_calls", Integer, nullable=False, default=0), Column("report_hash", String),
    Column("report_attempts", Integer, nullable=False, default=0, server_default="0"),
    Column("owner", String), Column("epoch", Integer, nullable=False, server_default="0"),
    Column("lease_expires_at", Float),
    Column("created_at", String, nullable=False), Column("updated_at", String, nullable=False),
    UniqueConstraint("project_id", "actor", "idempotency_key"),
    CheckConstraint(f"state IN ({RUN_STATES})", name="ck_aw_runs_state_v2"),
    CheckConstraint("report_attempts >= 0", name="ck_aw_runs_reports_v2"))
events = Table("aw_events", metadata,
    Column("run_id", String, primary_key=True), Column("seq", Integer, primary_key=True),
    Column("kind", String, nullable=False), Column("at", String, nullable=False),
    Column("payload", JSONB, nullable=False),
    ForeignKeyConstraint(["run_id"], ["aw_runs.id"], name="fk_aw_events_run_v2", deferrable=True, initially="DEFERRED"))
steps = Table("aw_steps", metadata,
    Column("id", String, primary_key=True), Column("run_id", String, nullable=False, index=True),
    Column("action_key", String, nullable=False), Column("input_digest", String, nullable=False),
    Column("attempt_id", String, nullable=False, unique=True), Column("state", String, nullable=False),
    Column("decision_hash", String, nullable=False), Column("evidence_id", String),
    Column("created_at", String, nullable=False),
    UniqueConstraint("run_id", "action_key"),
    UniqueConstraint("id", "run_id", name="uq_aw_steps_identity_v2"),
    CheckConstraint(f"state IN ({ATTEMPT_STATES})", name="ck_aw_steps_state_v2"),
    ForeignKeyConstraint(["run_id"], ["aw_runs.id"], name="fk_aw_steps_run_v2", deferrable=True, initially="DEFERRED"),
    ForeignKeyConstraint(["attempt_id", "id", "run_id"], ["aw_attempts.id", "aw_attempts.step_id", "aw_attempts.run_id"], name="fk_aw_steps_attempt_v2", deferrable=True, initially="DEFERRED"),
    ForeignKeyConstraint(["evidence_id", "id", "run_id"], ["aw_evidence.id", "aw_evidence.step_id", "aw_evidence.run_id"], name="fk_aw_steps_evidence_v2", deferrable=True, initially="DEFERRED"))
evidence = Table("aw_evidence", metadata,
    Column("id", String, primary_key=True), Column("run_id", String, nullable=False, index=True),
    Column("step_id", String, nullable=False, unique=True), Column("object_hash", String, nullable=False),
    Column("created_at", String, nullable=False),
    UniqueConstraint("id", "step_id", "run_id", name="uq_aw_evidence_identity_v2"),
    ForeignKeyConstraint(["run_id"], ["aw_runs.id"], name="fk_aw_evidence_run_v2", deferrable=True, initially="DEFERRED"),
    ForeignKeyConstraint(["step_id", "run_id"], ["aw_steps.id", "aw_steps.run_id"], name="fk_aw_evidence_step_v2", deferrable=True, initially="DEFERRED"))
attempts = Table("aw_attempts", metadata,
    Column("id", String, primary_key=True), Column("step_id", String, nullable=False),
    Column("run_id", String, nullable=False), Column("seq", Integer, nullable=False),
    Column("state", String, nullable=False), Column("outcome_class", String),
    Column("error_code", String), Column("error_object", String), Column("evidence_id", String),
    Column("started_at", String, nullable=False), Column("finished_at", String),
    UniqueConstraint("step_id", "seq", name="uq_aw_attempts_sequence_v2"),
    UniqueConstraint("id", "step_id", "run_id", name="uq_aw_attempts_identity_v2"),
    CheckConstraint(f"state IN ({ATTEMPT_STATES})", name="ck_aw_attempts_state_v2"),
    CheckConstraint("seq > 0", name="ck_aw_attempts_sequence_v2"),
    ForeignKeyConstraint(["run_id"], ["aw_runs.id"], name="fk_aw_attempts_run_v2", deferrable=True, initially="DEFERRED"),
    ForeignKeyConstraint(["step_id", "run_id"], ["aw_steps.id", "aw_steps.run_id"], name="fk_aw_attempts_step_v2", deferrable=True, initially="DEFERRED"),
    ForeignKeyConstraint(["evidence_id", "step_id", "run_id"], ["aw_evidence.id", "aw_evidence.step_id", "aw_evidence.run_id"], name="fk_aw_attempts_evidence_v2", deferrable=True, initially="DEFERRED"))
Index("uq_aw_attempts_one_accepted_v2", attempts.c.step_id, unique=True,
      postgresql_where=attempts.c.state == "ACCEPTED")
resolutions = Table("aw_resolutions", metadata,
    Column("id", String, primary_key=True), Column("attempt_id", String, nullable=False),
    Column("step_id", String, nullable=False), Column("run_id", String, nullable=False),
    Column("actor", String, nullable=False), Column("reason", String, nullable=False),
    Column("disposition", String, nullable=False), Column("created_at", String, nullable=False),
    CheckConstraint("disposition = 'confirm_no_side_effects_failed'", name="ck_aw_resolutions_disposition_v2"),
    ForeignKeyConstraint(["run_id"], ["aw_runs.id"], name="fk_aw_resolutions_run_v2", deferrable=True, initially="DEFERRED"),
    ForeignKeyConstraint(["attempt_id", "step_id", "run_id"], ["aw_attempts.id", "aw_attempts.step_id", "aw_attempts.run_id"], name="fk_aw_resolutions_attempt_v2", deferrable=True, initially="DEFERRED"))
releases = Table("aw_releases", metadata,
    Column("namespace", String, primary_key=True), Column("ref", String, primary_key=True),
    Column("content_hash", String, nullable=False))
schema_versions = Table("aw_schema_versions", metadata, Column("version", Integer, primary_key=True))
migration_journal = Table("aw_migration_journal", metadata,
    Column("to_version", Integer, primary_key=True), Column("from_version", Integer, nullable=False),
    Column("legacy_digest", String, nullable=False), Column("attempts_digest", String, nullable=False),
    Column("applied_at", String, nullable=False))

# Source run may live in the readonly archive, so deliberately no FK to local aw_runs.
revalidations = Table("aw_revalidations", metadata,
    Column("id", String, primary_key=True), Column("run_id", String, nullable=False),
    Column("source_database", String, nullable=False), Column("original_report_hash", String, nullable=False),
    Column("validator", String, nullable=False), Column("result_hash", String, nullable=False),
    Column("created_at", String, nullable=False))


# Mutable operational counters are deliberately separate from append-only events.
access_refusals = Table("aw_access_refusals", metadata,
    Column("run_id", String, primary_key=True),
    Column("target_run_id", String, primary_key=True),
    Column("count", Integer, nullable=False), Column("last_refused_at", String, nullable=False),
    CheckConstraint("count > 0", name="ck_aw_access_refusals_count_v4"),
    ForeignKeyConstraint(["run_id"], ["aw_runs.id"], name="fk_aw_access_refusals_run_v4"))


# Immutable invocation journal: one row per transition, never UPDATE/DELETE.
model_invocations = Table("aw_model_invocations", metadata,
    Column("invocation_id", String, primary_key=True), Column("seq", Integer, primary_key=True),
    Column("run_id", String, nullable=False), Column("epoch", Integer, nullable=False),
    Column("kind", String, nullable=False), Column("payload", JSONB, nullable=False),
    Column("at", String, nullable=False),
    ForeignKeyConstraint(["run_id"], ["aw_runs.id"], name="fk_aw_model_invocations_run_v5"),
    CheckConstraint("seq > 0", name="ck_aw_model_invocations_seq_v5"))
# Operational budget accounts may change. Costs are integer micro-currency units.
budget_accounts = Table("aw_budget_accounts", metadata,
    Column("id", String, primary_key=True), Column("config", JSONB, nullable=False),
    Column("used", JSONB, nullable=False), Column("held", JSONB, nullable=False),
    Column("halted", String))
run_execution = Table("aw_run_execution", metadata,
    Column("run_id", String, primary_key=True), Column("config", JSONB, nullable=False),
    Column("started_at", Float), Column("calls", Integer, nullable=False, server_default="0"),
    Column("input_tokens", Integer, nullable=False, server_default="0"),
    Column("output_tokens", Integer, nullable=False, server_default="0"),
    Column("cost_micros", Integer, nullable=False, server_default="0"),
    ForeignKeyConstraint(["run_id"], ["aw_runs.id"], name="fk_aw_run_execution_run_v5"))


# Snapshot metadata is separate from source records and CAS report evidence.
dataset_versions = Table("aw_dataset_versions", metadata,
    Column("id", String, primary_key=True), Column("privacy", String, nullable=False),
    Column("state", String, nullable=False), Column("manifest", JSONB),
    Column("created_at", String, nullable=False),
    CheckConstraint("state IN ('STAGING','AVAILABLE','RETIRED','EXPIRED')", name="ck_aw_dataset_state_v6"))
snapshot_sets = Table("aw_snapshot_sets", metadata,
    Column("id", String, primary_key=True), Column("dataset_id", String, nullable=False),
    Column("state", String, nullable=False), Column("manifest_hash", String),
    Column("created_at", String, nullable=False),
    ForeignKeyConstraint(["dataset_id"], ["aw_dataset_versions.id"], name="fk_aw_snapshot_dataset_v6"),
    CheckConstraint("state IN ('STAGING','AVAILABLE','RETIRED','EXPIRED')", name="ck_aw_snapshot_state_v6"))


daily_sessions = Table("aw_daily_sessions", metadata,
    Column("run_id",String,primary_key=True), Column("config",JSONB,nullable=False),
    Column("position",Integer,nullable=False,server_default="0"),
    ForeignKeyConstraint(["run_id"],["aw_runs.id"],name="fk_aw_daily_run_v7"))
decisions = Table("aw_decisions", metadata,
    Column("run_id",String,primary_key=True),Column("day",String,primary_key=True),
    Column("object_hash",String,nullable=False),Column("created_at",String,nullable=False),
    ForeignKeyConstraint(["run_id"],["aw_runs.id"],name="fk_aw_decision_run_v7"))
decision_reviews = Table("aw_decision_reviews", metadata,
    Column("id",String,primary_key=True),Column("run_id",String,nullable=False),Column("day",String,nullable=False),
    Column("object_hash",String,nullable=False),Column("created_at",String,nullable=False),
    ForeignKeyConstraint(["run_id","day"],["aw_decisions.run_id","aw_decisions.day"],name="fk_aw_review_decision_v7"))


class Objects:
    @staticmethod
    def check_class(root: Path, expected: str):
        try:
            actual = (runtime_path(root) / ".store-class").read_text().strip()
        except (OSError, UnicodeError):
            raise WorkbenchError("store_class_missing", "Explicitly initialize the object store class before use", 403) from None
        if actual != expected or expected not in {"public", "private"}:
            raise WorkbenchError("store_class_mismatch", "Object store class does not match this entry point", 403)
        return actual

    @staticmethod
    def initialize(root: Path, store_class: str):
        if store_class not in {"public", "private"}:
            raise WorkbenchError("invalid_store_class", "Choose public or private")
        root = runtime_path(root)
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
        marker = root / ".store-class"
        try:
            fd = os.open(marker, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            return Objects.check_class(root, store_class)
        with os.fdopen(fd, "w") as f:
            f.write(store_class + "\n")
            f.flush()
            os.fsync(f.fileno())
        return store_class

    def __init__(self, root: Path):
        self.root = runtime_path(root)
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.root.chmod(0o700)
        marker = self.root / ".store-class"
        self.store_class = marker.read_text().strip() if marker.is_file() else "unclassified"

    def _path(self, object_hash: str) -> Path:
        if not re.fullmatch(r"[a-f0-9]{64}", object_hash):
            raise WorkbenchError("invalid_object", "Invalid object identifier")
        return runtime_path(self.root / (object_hash + ".json"))

    def put(self, value) -> str:
        data = canonical(value)
        key = hashlib.sha256(data).hexdigest()
        target = self._path(key)
        fd, tmp = tempfile.mkstemp(prefix=".staging-", dir=self.root)
        try:
            with os.fdopen(fd, "wb") as f:
                f.write(data)
                f.flush()
                os.fsync(f.fileno())
            os.chmod(tmp, 0o400)
            try:
                os.link(tmp, target)  # Exclusive publication; never overwrite evidence.
            except FileExistsError:
                pass
            d = os.open(self.root, os.O_RDONLY)
            try:
                os.fsync(d)
            finally:
                os.close(d)
            self.get(key)  # A corrupt existing object is not silently repaired.
            return key
        finally:
            os.unlink(tmp)

    def get(self, key: str):
        try:
            data = self._path(key).read_bytes()
        except FileNotFoundError:
            raise WorkbenchError("artifact_missing", "Referenced artifact is unavailable", 409) from None
        if hashlib.sha256(data).hexdigest() != key:
            raise WorkbenchError("artifact_corrupt", "Artifact checksum mismatch", 409)
        return json.loads(data)


class Store:
    def __init__(self, url: str | URL):
        if make_url(url).drivername != "postgresql+psycopg":
            raise ValueError("The workbench metadata store requires PostgreSQL/psycopg")
        self.engine = create_engine(url, pool_pre_ping=True, pool_size=3, max_overflow=2)

    @staticmethod
    def _read_version(c):
        tables = set(c.execute(text("SELECT tablename FROM pg_catalog.pg_tables "
                                    "WHERE schemaname = 'public' AND left(tablename, 3) = 'aw_'" )).scalars())
        present = c.execute(text("SELECT to_regclass('public.aw_schema_versions')")).scalar_one()
        if present is None:
            if tables:
                raise WorkbenchError("unsupported_schema", "Metadata schema is incomplete; explicit inspection is required", 409)
            return 0
        versions = c.execute(select(schema_versions.c.version)).scalars().all()
        if len(versions) != 1 or versions[0] not in {1, 2, 3, 4, 5, 6, 7}:
            raise WorkbenchError("unsupported_schema", "Unknown metadata schema version; no changes were made", 409)
        required = {"aw_runs", "aw_events", "aw_steps", "aw_evidence", "aw_releases", "aw_schema_versions"}
        if versions[0] >= 2:
            required |= {"aw_attempts", "aw_resolutions", "aw_migration_journal"}
        if versions[0] >= 3:
            required.add("aw_revalidations")
        if versions[0] >= 4:
            required.add("aw_access_refusals")
        if versions[0] >= 5:
            required |= {"aw_model_invocations", "aw_budget_accounts", "aw_run_execution"}
        if versions[0] >= 6:
            required |= {"aw_dataset_versions", "aw_snapshot_sets"}
        if versions[0] >= 7:
            required |= {"aw_daily_sessions", "aw_decisions", "aw_decision_reviews"}
        if not required.issubset(tables):
            raise WorkbenchError("unsupported_schema", "Metadata schema is incomplete; explicit inspection is required", 409)
        return versions[0]

    def read_version(self):
        """Inspect schema identity without creating or changing any object."""
        with self.engine.connect() as c:
            return self._read_version(c)

    def check_version(self, expected_version=SCHEMA_VERSION, *, legacy_readonly=False):
        version = self.read_version()
        if legacy_readonly and version == 2:
            return version
        if version != expected_version:
            raise WorkbenchError("migration_required", "Metadata schema requires an explicit migration before startup", 409)
        return version

    def migrate(self, *, expected_database: str, from_version: int, to_version: int):
        """Explicit, transactional migration with strict source-version matching.

        A repeated 1→2 request is rejected once version 2 exists. Callers may
        explicitly request 2→2 to verify identity and obtain an idempotent no-op.
        """
        with self.engine.begin() as c:
            database = c.execute(text("SELECT current_database()")).scalar_one()
            if not expected_database or database != expected_database:
                raise WorkbenchError("database_mismatch", "Migration database does not match the expected name", 409)
            c.execute(text("SELECT pg_advisory_xact_lock(72408191)"))
            version = self._read_version(c)
            if version != from_version:
                raise WorkbenchError("migration_version_mismatch", "Current schema does not match the required source version", 409)
            if (from_version, to_version) not in {(start, end) for start in range(8) for end in range(max(1, start), 8)}:
                raise WorkbenchError("unsupported_schema", "Requested schema migration is unsupported", 409)
            changed = version != to_version
            from .migrations import upgrade
            while version < to_version:
                upgrade(c, version, version + 1)
                version += 1
            return {"metadata_schema": to_version, "from_version": from_version,
                    "to_version": to_version, "changed": changed, "source_mutations": 0}

    def rollback(self, *, expected_database: str, from_version=2, to_version=1):
        """Explicit rollback is allowed only while all v1 records are unchanged."""
        with self.engine.begin() as c:
            database = c.execute(text("SELECT current_database()")).scalar_one()
            if not expected_database or database != expected_database:
                raise WorkbenchError("database_mismatch", "Rollback database does not match the expected name", 409)
            c.execute(text("SELECT pg_advisory_xact_lock(72408191)"))
            version = self._read_version(c)
            if version != from_version:
                raise WorkbenchError("migration_version_mismatch", "Current schema does not match the required source version", 409)
            if (from_version, to_version) == (7, 6):
                for table in (decision_reviews, decisions, daily_sessions):
                    if c.execute(select(table).limit(1)).first():
                        raise WorkbenchError("rollback_unsafe", "Daily history must be preserved", 409)
                for table in (decision_reviews, decisions, daily_sessions):table.drop(c)
                c.execute(update(schema_versions).values(version=6))
                return {"metadata_schema":6,"from_version":7,"to_version":6,"changed":True,"source_mutations":0,"legacy_records_unchanged":True}
            if (from_version, to_version) == (6, 5):
                for table in (snapshot_sets, dataset_versions):
                    if c.execute(select(table).limit(1)).first():
                        raise WorkbenchError("rollback_unsafe", "Snapshot records must be preserved", 409)
                snapshot_sets.drop(c); dataset_versions.drop(c)
                c.execute(update(schema_versions).values(version=5))
                return {"metadata_schema":5,"from_version":6,"to_version":5,"changed":True,"source_mutations":0,"legacy_records_unchanged":True}
            if (from_version, to_version) == (5, 4):
                for table in (model_invocations, budget_accounts, run_execution):
                    if c.execute(select(table).limit(1)).first():
                        raise WorkbenchError("rollback_unsafe", "Execution records must be preserved", 409)
                if c.execute(select(runs.c.id).where((runs.c.owner.is_not(None)) | (runs.c.epoch != 0) | runs.c.lease_expires_at.is_not(None)).limit(1)).first():
                    raise WorkbenchError("rollback_unsafe", "Lease history must be preserved", 409)
                for table in (model_invocations, run_execution, budget_accounts):table.drop(c)
                c.execute(text("ALTER TABLE aw_runs DROP COLUMN owner, DROP COLUMN epoch, DROP COLUMN lease_expires_at"))
                c.execute(update(schema_versions).values(version=4))
                return {"metadata_schema":4,"from_version":5,"to_version":4,"changed":True,"source_mutations":0,"legacy_records_unchanged":True}
            if (from_version, to_version) == (4, 3):
                if c.execute(select(access_refusals.c.run_id).limit(1)).first():
                    raise WorkbenchError("rollback_refused", "Access refusal counters must be preserved", 409)
                access_refusals.drop(c)
                c.execute(update(schema_versions).values(version=3))
                return {"metadata_schema": 3, "from_version": 4, "to_version": 3,
                        "changed": True, "source_mutations": 0, "legacy_records_unchanged": True}
            if (from_version, to_version) == (3, 2):
                from .migrations import downgrade_v3
                downgrade_v3(c)
                return {"metadata_schema": 2, "from_version": 3, "to_version": 2,
                        "changed": True, "source_mutations": 0, "legacy_records_unchanged": True}
            if (from_version, to_version) != (2, 1):
                raise WorkbenchError("unsupported_schema", "Requested schema rollback is unsupported", 409)
            from .migrations import downgrade
            downgrade(c)
            return {"metadata_schema": 1, "from_version": 2, "to_version": 1,
                    "changed": True, "source_mutations": 0, "legacy_records_unchanged": True}

    def event(self, c, run_id: str, kind: str, payload: dict):
        seq = c.execute(update(runs).where(runs.c.id == run_id).values(
            seq=runs.c.seq + 1, updated_at=now()).returning(runs.c.seq)).scalar_one()
        c.execute(insert(events).values(run_id=run_id, seq=seq, kind=kind, at=now(), payload=payload))

    def release(self, c, namespace: str, ref: str, content_hash: str):
        # Serialize registration in this initial migration, including concurrent first use.
        c.execute(text("SELECT pg_advisory_xact_lock(72408192)"))
        old = c.execute(select(releases).where(releases.c.namespace == namespace,
                                              releases.c.ref == ref)).mappings().first()
        if old and old["content_hash"] != content_hash:
            raise WorkbenchError("immutable_release", "This release reference already has different content", 409)
        if not old:
            c.execute(insert(releases).values(namespace=namespace, ref=ref, content_hash=content_hash))
