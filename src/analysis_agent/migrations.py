"""Explicit transactional schema steps; no connection or migration at import time.

The rollback journal stores digests, not copies of task rows or CAS objects.
Rollback refuses to discard any state written after the v1→v2 migration.
"""
from __future__ import annotations

import hashlib

from sqlalchemy import insert, select, text
from sqlalchemy.schema import AddConstraint, DropConstraint

from .contracts import WorkbenchError, canonical, now


LEGACY_COLUMNS = {
    "aw_runs": "id project_id actor idempotency_key request_digest manifest state seq tool_calls report_hash created_at updated_at".split(),
    "aw_events": "run_id seq kind at payload".split(),
    "aw_steps": "id run_id action_key input_digest attempt_id state decision_hash evidence_id created_at".split(),
    "aw_evidence": "id run_id step_id object_hash created_at".split(),
    "aw_releases": "namespace ref content_hash".split(),
}
LEGACY_ORDER = {"aw_runs": "id", "aw_events": "run_id, seq", "aw_steps": "id",
                "aw_evidence": "id", "aw_releases": "namespace, ref"}

V1_DDL = (
    """CREATE TABLE aw_runs (
        id VARCHAR PRIMARY KEY, project_id VARCHAR NOT NULL, actor VARCHAR NOT NULL,
        idempotency_key VARCHAR NOT NULL, request_digest VARCHAR NOT NULL, manifest JSONB NOT NULL,
        state VARCHAR NOT NULL, seq INTEGER NOT NULL, tool_calls INTEGER NOT NULL,
        report_hash VARCHAR, created_at VARCHAR NOT NULL, updated_at VARCHAR NOT NULL,
        UNIQUE(project_id, actor, idempotency_key))""",
    """CREATE TABLE aw_events (run_id VARCHAR NOT NULL, seq INTEGER NOT NULL,
        kind VARCHAR NOT NULL, at VARCHAR NOT NULL, payload JSONB NOT NULL, PRIMARY KEY(run_id, seq))""",
    """CREATE TABLE aw_steps (id VARCHAR PRIMARY KEY, run_id VARCHAR NOT NULL,
        action_key VARCHAR NOT NULL, input_digest VARCHAR NOT NULL, attempt_id VARCHAR NOT NULL UNIQUE,
        state VARCHAR NOT NULL, decision_hash VARCHAR NOT NULL, evidence_id VARCHAR,
        created_at VARCHAR NOT NULL, UNIQUE(run_id, action_key))""",
    "CREATE INDEX ix_aw_steps_run_id ON aw_steps(run_id)",
    """CREATE TABLE aw_evidence (id VARCHAR PRIMARY KEY, run_id VARCHAR NOT NULL,
        step_id VARCHAR NOT NULL UNIQUE, object_hash VARCHAR NOT NULL, created_at VARCHAR NOT NULL)""",
    "CREATE INDEX ix_aw_evidence_run_id ON aw_evidence(run_id)",
    """CREATE TABLE aw_releases (namespace VARCHAR NOT NULL, ref VARCHAR NOT NULL,
        content_hash VARCHAR NOT NULL, PRIMARY KEY(namespace, ref))""",
    "CREATE TABLE aw_schema_versions (version INTEGER PRIMARY KEY)",
    "INSERT INTO aw_schema_versions(version) VALUES (1)",
)


def legacy_digest(connection):
    """Hash v1 columns in deterministic order without exporting task contents."""
    hashes = {}
    for table, columns in LEGACY_COLUMNS.items():
        statement = f"SELECT {', '.join(columns)} FROM {table} ORDER BY {LEGACY_ORDER[table]}"
        digest = hashlib.sha256()
        for row in connection.execute(text(statement)).mappings():
            payload = canonical(dict(row))
            digest.update(len(payload).to_bytes(8, "big"))
            digest.update(payload)
        hashes[table] = digest.hexdigest()
    return hashlib.sha256(canonical(hashes)).hexdigest()


def _attempts_digest(connection):
    from .storage import attempts
    digest = hashlib.sha256()
    for row in connection.execute(select(attempts).order_by(attempts.c.id)).mappings():
        payload = canonical(dict(row))
        digest.update(len(payload).to_bytes(8, "big"))
        digest.update(payload)
    return digest.hexdigest()


def _old_table_constraints():
    from .storage import evidence, events, runs, steps
    constraints = [constraint for table in (runs, events, steps, evidence)
                   for constraint in table.constraints
                   if constraint.name and constraint.name.endswith("_v2")]
    # Referenced composite UNIQUE constraints must exist before their FKs.
    return sorted(constraints, key=lambda constraint: (
        0 if constraint.name.startswith("uq_") else
        2 if constraint.name.startswith("fk_") else 1,
        constraint.name))


def _lock(connection, *, v2=False):
    names = list(LEGACY_COLUMNS) + ["aw_schema_versions"]
    if v2:
        names += ["aw_attempts", "aw_resolutions", "aw_migration_journal"]
    connection.execute(text("LOCK TABLE " + ", ".join(names) + " IN ACCESS EXCLUSIVE MODE"))


def upgrade(connection, from_version, to_version):
    """Execute exactly one known step inside the caller's existing transaction."""
    if (from_version, to_version) == (6, 7):
        from .storage import daily_sessions, decisions, decision_reviews, schema_versions
        for table in (daily_sessions, decisions, decision_reviews):table.create(connection)
        connection.execute(schema_versions.update().where(schema_versions.c.version == 6).values(version=7))
        return
    if (from_version, to_version) == (5, 6):
        from .storage import dataset_versions, snapshot_sets, schema_versions
        dataset_versions.create(connection); snapshot_sets.create(connection)
        connection.execute(schema_versions.update().where(schema_versions.c.version == 5).values(version=6))
        return
    if (from_version, to_version) == (4, 5):
        from .storage import model_invocations, budget_accounts, run_execution, schema_versions
        _lock(connection, v2=True)
        connection.execute(text("ALTER TABLE aw_runs ADD COLUMN owner VARCHAR, ADD COLUMN epoch INTEGER NOT NULL DEFAULT 0, ADD COLUMN lease_expires_at DOUBLE PRECISION"))
        for table in (budget_accounts, run_execution, model_invocations):table.create(connection)
        connection.execute(schema_versions.update().where(schema_versions.c.version == 4).values(version=5))
        return
    if (from_version, to_version) == (3, 4):
        from .storage import access_refusals, schema_versions
        _lock(connection, v2=True)
        access_refusals.create(connection)
        connection.execute(schema_versions.update().where(schema_versions.c.version == 3).values(version=4))
        return
    if (from_version, to_version) == (2, 3):
        from .storage import revalidations, schema_versions
        _lock(connection, v2=True)
        revalidations.create(connection)
        connection.execute(schema_versions.update().where(schema_versions.c.version == 2).values(version=3))
        return
    if (from_version, to_version) == (0, 1):
        for statement in V1_DDL:
            connection.execute(text(statement))
        return
    if (from_version, to_version) != (1, 2):
        raise WorkbenchError("unsupported_schema", "Unknown schema migration step", 409)

    from .storage import attempts, migration_journal, resolutions, schema_versions
    _lock(connection)
    before_digest = legacy_digest(connection)
    connection.execute(text("ALTER TABLE aw_runs ADD COLUMN report_attempts INTEGER NOT NULL DEFAULT 0"))
    constraints = _old_table_constraints()
    for constraint in constraints:
        if constraint.name.startswith("uq_"):
            connection.execute(AddConstraint(constraint))

    # Cyclic step→attempt and step→evidence FKs are added after backfill. All
    # relation FKs are initially deferred, so ordinary writes may insert a step
    # and its attempt together without inventing a partially committed record.
    attempts.create(connection)
    resolutions.create(connection)
    migration_journal.create(connection)
    connection.execute(text("""INSERT INTO aw_attempts (
        id, step_id, run_id, seq, state, outcome_class, error_code, error_object,
        evidence_id, started_at, finished_at)
        SELECT s.attempt_id, s.id, s.run_id, 1, s.state,
            CASE s.state WHEN 'ACCEPTED' THEN 'accepted'
                         WHEN 'UNKNOWN' THEN 'uncertain'
                         ELSE NULL END,
            CASE WHEN s.state = 'UNKNOWN' THEN 'legacy_unknown' ELSE NULL END,
            NULL, s.evidence_id, s.created_at,
            CASE WHEN s.state = 'ACCEPTED' THEN e.created_at ELSE NULL END
        FROM aw_steps s LEFT JOIN aw_evidence e ON e.id = s.evidence_id"""))
    connection.execute(text("SET CONSTRAINTS ALL IMMEDIATE"))
    for constraint in constraints:
        if not constraint.name.startswith("uq_"):
            connection.execute(AddConstraint(constraint))
    # Force validation now; neither the schema marker nor a partial migration
    # survives if legacy relationships are invalid.
    connection.execute(text("SET CONSTRAINTS ALL IMMEDIATE"))
    if legacy_digest(connection) != before_digest:
        raise WorkbenchError("migration_changed_records", "Migration changed legacy records; transaction rolled back", 409)
    connection.execute(insert(migration_journal).values(
        to_version=2, from_version=1, legacy_digest=before_digest,
        attempts_digest=_attempts_digest(connection), applied_at=now()))
    connection.execute(schema_versions.update().where(schema_versions.c.version == 1).values(version=2))


def downgrade(connection):
    """Return to v1 only when every legacy record and backfilled attempt is intact.

    This deliberately refuses a rollback after v2 analysis activity, including
    new/deleted runs, changed events, resolutions, or report submissions. It does
    not silently drop v2-only state or manufacture v1 history.
    """
    from .storage import attempts, migration_journal, resolutions, runs, schema_versions
    _lock(connection, v2=True)
    journals = connection.execute(select(migration_journal)).mappings().all()
    unchanged = (
        len(journals) == 1 and journals[0]["from_version"] == 1
        and journals[0]["to_version"] == 2
        and journals[0]["legacy_digest"] == legacy_digest(connection)
        and journals[0]["attempts_digest"] == _attempts_digest(connection)
        and connection.execute(select(resolutions.c.id).limit(1)).first() is None
        and connection.execute(select(runs.c.id).where(runs.c.report_attempts != 0).limit(1)).first() is None
    )
    if not unchanged:
        raise WorkbenchError("rollback_unsafe", "Schema v2 activity exists; rollback would discard state and was refused", 409)
    constraints = _old_table_constraints()
    for constraint in reversed(constraints):
        if not constraint.name.startswith("uq_"):
            connection.execute(DropConstraint(constraint))
    resolutions.drop(connection)
    attempts.drop(connection)
    migration_journal.drop(connection)
    for constraint in reversed(constraints):
        if constraint.name.startswith("uq_"):
            connection.execute(DropConstraint(constraint))
    connection.execute(text("ALTER TABLE aw_runs DROP COLUMN report_attempts"))
    connection.execute(schema_versions.update().where(schema_versions.c.version == 2).values(version=1))
    if legacy_digest(connection) != journals[0]["legacy_digest"]:
        raise WorkbenchError("rollback_changed_records", "Rollback changed legacy records; transaction rolled back", 409)


def downgrade_v3(connection):
    """Never discard revalidation records: only an empty v3 table can be removed."""
    from .storage import revalidations, schema_versions
    _lock(connection, v2=True)
    connection.execute(text("LOCK TABLE aw_revalidations IN ACCESS EXCLUSIVE MODE"))
    if connection.execute(select(revalidations.c.id).limit(1)).first():
        raise WorkbenchError("rollback_unsafe", "Revalidation records exist; v3 rollback refused", 409)
    revalidations.drop(connection)
    connection.execute(schema_versions.update().where(schema_versions.c.version == 3).values(version=2))
