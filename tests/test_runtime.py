"""Real PostgreSQL integration checks for this execution slice.

Set WORKBENCH_TEST_DB_URL_FILE to a private file containing a
postgresql+psycopg URL for the approved workbench_test_b1main database.
Otherwise every test here is explicitly skipped. The server's database
name and independent instance directory are checked before any fixture writes.
Test setup does not migrate: initialization is an explicit operation.
These checks are a bounded regression suite, not the full blueprint's
concurrency, recovery, provider, deployment, or 58-item acceptance campaign.
Only synthetic test projects and temporary object files are created.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
import os
import uuid

import pytest
from sqlalchemy import delete, select

from analysis_agent.contracts import (
    EvidenceRef, Fact, ResultSpec, RequiredFact, Registry, Report, RunRequest, SkillRelease, TableResult,
    ToolSpec, WorkbenchError,
)
from analysis_agent.runtime import Workbench
from analysis_agent.storage import Objects, Store, daily_sessions, decisions, decision_reviews, access_refusals, model_invocations, run_execution, attempts, resolutions, evidence, events, releases, runs, steps
from database_guard import open_test_store, reopen_test_store


class TinyAdapter:
    """Synthetic, independent fixture; no demo/production adapter dependency."""

    ref = "integration_fixture@1"
    data_contract = "integration_counter@1"
    privacy = "public"

    def __init__(self):
        self.execute_calls = 0
        self.catalog_revision = 1
        self.advertise_frozen = False
        self.fail_execution = False
        self.tools = {"read_counter@2": ToolSpec(side_effects="none", retry="new_action_allowed", 
            ref="read_counter@2", capability="read_counter", description="Read a synthetic counter",
            input_schema={
                "type": "object", "additionalProperties": False,
                "required": ["entity", "day"],
                "properties": {
                    "entity": {"type": "string", "enum": ["alpha", "beta"]},
                    "day": {"type": "string", "pattern": r"^2026-01-0[1-9]$"},
                },
            },
        )}

    def catalog(self):
        return {"contract": self.data_contract, "revision": self.catalog_revision,
                "entities": ["alpha", "beta"], "columns": ["entity", "day", "requests", "errors"]}

    def capabilities(self):
        return {"live": True, "frozen": self.advertise_frozen}

    def validate_task(self, parameters):
        if parameters != {"first_day": "2026-01-02", "last_day": "2026-01-03", "entities": ["alpha"]}:
            raise ValueError("This fixture has one exact synthetic task scope")

    def validate_call(self, tool_ref, args, parameters):
        if args["entity"] not in parameters["entities"]:
            raise ValueError("Entity is outside the admitted task")
        if not parameters["first_day"] <= args["day"] <= parameters["last_day"]:
            raise ValueError("Date is outside the admitted task")

    def execute(self, tool_ref, args, parameters):
        self.execute_calls += 1
        if self.fail_execution:
            raise RuntimeError("Synthetic source unavailable; no private text")
        return TableResult(
            rows=[{"entity": args["entity"], "day": args["day"], "requests": 12, "errors": 3}],
            units={"entity": "identifier", "day": "date", "requests": "count", "errors": "count"},
            time_range={"start": args["day"], "end": args["day"]},
            entity_keys=["entity"], completeness="complete_for_query",
            provenance={"source": "independent_synthetic_test_fixture"},
        )


@pytest.fixture(scope="module")
def real_store():
    filename = os.environ.get("WORKBENCH_TEST_DB_URL_FILE")
    if not filename:
        pytest.skip("Real PostgreSQL integration requires WORKBENCH_TEST_DB_URL_FILE")
    store = open_test_store(filename)
    try:
        yield store
    finally:
        store.engine.dispose()


@dataclass
class Harness:
    store: Store
    objects: Objects
    adapter: TinyAdapter
    registry: Registry
    workbench: Workbench
    project: str
    actor: str
    request: RunRequest

    def create(self, **updates):
        request = self.request.model_copy(update=updates)
        return self.workbench.create(request, "request-" + uuid.uuid4().hex)["run_id"]

    def call(self, run_id, *, key="counter-1", args=None, explanation="Read the admitted counter"):
        return self.workbench.call(run_id, "read_counter@2", args or {"entity": "alpha", "day": "2026-01-02"}, key, explanation)

    def row(self, run_id):
        with self.store.engine.connect() as connection:
            return dict(connection.execute(select(runs).where(runs.c.id == run_id)).mappings().one())

    def event_rows(self, run_id):
        with self.store.engine.connect() as connection:
            return [dict(row) for row in connection.execute(
                select(events).where(events.c.run_id == run_id).order_by(events.c.seq)).mappings()]


@pytest.fixture
def harness(real_store, tmp_path):
    project = "integration-" + uuid.uuid4().hex
    actor = "test-actor-" + uuid.uuid4().hex
    adapter = TinyAdapter()
    skill = SkillRelease(ref="integration_skill@2", instructions="Read the registered synthetic counter.",
                         data_contract=adapter.data_contract, tools=("read_counter@2",))
    registry = Registry([adapter], [skill])
    objects = Objects(tmp_path / "objects")
    workbench = Workbench(real_store, objects, registry, actor, {project: [adapter.ref]})
    request = RunRequest(
        project_id=project, source_ref=adapter.ref, skill_ref=skill.ref,
        question="How many requests were observed for the admitted entity and day?",
        parameters={"first_day": "2026-01-02", "last_day": "2026-01-03", "entities": ["alpha"]},
        result_spec=ResultSpec(requirements=[RequiredFact(column="requests")]), tool_budget=2,
    )
    yield Harness(real_store, objects, adapter, registry, workbench, project, actor, request)
    # Clean only IDs and release namespaces owned by this test project.
    with real_store.engine.begin() as connection:
        ids = list(connection.execute(select(runs.c.id).where(runs.c.project_id == project)).scalars())
        if ids:
            for table in (decision_reviews, decisions, daily_sessions, model_invocations, run_execution, access_refusals, resolutions, attempts, evidence, steps, events):
                connection.execute(delete(table).where(table.c.run_id.in_(ids)))
            connection.execute(delete(runs).where(runs.c.id.in_(ids)))
        connection.execute(delete(releases).where(releases.c.namespace.in_(
            [project + suffix for suffix in (":source", ":skill", ":tool")])) )


def valid_report(item) -> Report:
    return Report(result_status="complete", title="Synthetic counter observation", facts=[Fact(
        refs=[EvidenceRef(evidence_id=item["evidence_id"], row=0, column="requests")],
        value=12, unit="count", entity={"entity": "alpha"},
        time_range=item["time_range"], label="",
    )])


def assert_error(code, function, *args, **kwargs):
    with pytest.raises(WorkbenchError) as exc:
        function(*args, **kwargs)
    assert exc.value.code == code
    return exc.value


def test_request_idempotency_is_persisted_and_conflicts_are_rejected(harness):
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: harness.workbench.create(harness.request, "same-request"), range(2)))
    assert len({result["run_id"] for result in results}) == 1
    assert sorted(result["created"] for result in results) == [False, True]
    run_id = results[0]["run_id"]
    changed = harness.request.model_copy(update={"question": "A materially different question"})
    assert_error("idempotency_conflict", harness.workbench.create, changed, "same-request")
    assert_error("idempotency_conflict", harness.workbench.create, harness.request, "same-request", driver="policy_fixture")
    assert [row["kind"] for row in harness.event_rows(run_id)] == ["RUN_ADMITTED", "SKILL_DELIVERED", "SKILL_DELIVERED"]


@pytest.mark.parametrize("change", ["data_contract", "missing_tool"])
def test_incompatible_skill_is_rejected_before_a_run_exists(harness, change):
    skill = harness.registry.skills[harness.request.skill_ref]
    updates = {"data_contract": "another_contract@1"} if change == "data_contract" else {"tools": ("unregistered@1",)}
    harness.registry.skills[skill.ref] = skill.model_copy(update=updates)
    assert_error("incompatible", harness.create)
    assert harness.workbench.history(harness.project) == []
    assert harness.adapter.execute_calls == 0


@pytest.mark.parametrize("adapter_advertises_frozen", [False, True])
def test_frozen_is_explicitly_unsupported_without_snapshot_implementation(harness, adapter_advertises_frozen):
    harness.adapter.advertise_frozen = adapter_advertises_frozen
    assert_error("unsupported", harness.create, consistency="frozen")
    assert harness.workbench.history(harness.project) == []


@pytest.mark.parametrize("args", [
    {"entity": "alpha'; DROP TABLE anything; --", "day": "2026-01-02"},
    {"entity": "alpha", "day": "2026-01-02", "sql": "SELECT * FROM secret"},
    {"entity": "alpha", "day": "2026-01-02", "project_id": "someone_else"},
    {"entity": "alpha", "day": "2026-01-02", "first_day": "1900-01-01"},
    {"entity": "beta", "day": "2026-01-02"},
    {"entity": "alpha", "day": "2026-01-09"},
])
def test_injection_and_scope_expansion_are_rejected_and_audited(harness, args):
    run_id = harness.create()
    assert_error("invalid_parameters", harness.call, run_id, args=args)
    assert harness.adapter.execute_calls == 0
    assert harness.row(run_id)["tool_calls"] == 0
    rejection = harness.event_rows(run_id)[-1]
    assert rejection["kind"] == "TOOL_REJECTED"
    assert rejection["payload"]["code"] == "invalid_parameters"
    assert "sql" not in rejection["payload"]


def test_tool_action_key_idempotency_and_budget_are_persisted(harness):
    run_id = harness.create(tool_budget=1)
    first = harness.call(run_id)
    repeated = harness.call(run_id)
    assert repeated == first
    assert harness.adapter.execute_calls == 1
    assert harness.row(run_id)["tool_calls"] == 1
    assert_error("idempotency_conflict", harness.call, run_id,
                 args={"entity": "alpha", "day": "2026-01-03"})
    assert_error("budget_exceeded", harness.call, run_id, key="counter-2")
    assert harness.row(run_id)["tool_calls"] == 1
    assert harness.adapter.execute_calls == 1
    audit = harness.event_rows(run_id)
    assert [row["kind"] for row in audit].count("EVIDENCE_ACCEPTED") == 1
    assert audit[-1]["payload"]["code"] == "budget_exceeded"
    assert [row["seq"] for row in audit] == list(range(1, len(audit) + 1))


@pytest.mark.parametrize("update, expected_code", [
    ({"value": 13}, "value_mismatch"),
    ({"value": 12.0}, "value_mismatch"),
    ({"unit": "percent"}, "unit_mismatch"),
    ({"entity": {"entity": "beta"}}, "wrong_entity"),
    ({"time_range": {"start": "2026-01-03", "end": "2026-01-03"}}, "wrong_time_range"),
])
def test_false_facts_cannot_publish_a_final_report(harness, update, expected_code):
    run_id = harness.create()
    item = harness.call(run_id)
    report = valid_report(item)
    report = report.model_copy(update={"facts": [report.facts[0].model_copy(update=update)]})
    candidate = harness.workbench.finalize(run_id, report)
    assert candidate["validation"]["status"] == "blocked"
    assert expected_code in [error["code"] for error in candidate["validation"]["errors"]]
    row = harness.row(run_id)
    assert row["state"] == "RUNNING" and row["report_hash"] is None
    kinds = [event["kind"] for event in harness.event_rows(run_id)]
    assert kinds[-1] == "REPORT_BLOCKED" and "REPORT_PUBLISHED" not in kinds


def test_cross_run_evidence_is_rejected_even_for_the_same_actor_and_project(harness):
    first_run, second_run = harness.create(), harness.create()
    first_evidence = harness.call(first_run)
    harness.call(second_run)
    assert_error("invalid_evidence", harness.workbench.read_evidence, second_run, first_evidence["evidence_id"])
    candidate = harness.workbench.finalize(second_run, valid_report(first_evidence))
    assert candidate["validation"]["status"] == "blocked"
    assert harness.row(second_run)["report_hash"] is None


def test_terminal_finalize_rejects_all_new_submissions_without_events(harness):
    run_id = harness.create()
    report = valid_report(harness.call(run_id))
    published = harness.workbench.finalize(run_id, report)
    assert published["validation"]["status"] == "valid"
    assert harness.row(run_id)["state"] == "SUCCEEDED"
    before = harness.event_rows(run_id)
    assert_error("invalid_state", harness.workbench.finalize, run_id, report)
    assert_error("invalid_state", harness.workbench.finalize, run_id,
                 report.model_copy(update={"title": "Different report content"}))
    assert harness.event_rows(run_id) == before
    assert [row["kind"] for row in before].count("REPORT_PUBLISHED") == 1


def test_full_reopen_needs_no_adapter_or_source_after_process_reconstruction(harness, monkeypatch):
    run_id = harness.create()
    item = harness.call(run_id)
    published = harness.workbench.finalize(run_id, valid_report(item))
    def source_must_not_run(*args, **kwargs):
        pytest.fail("Replay called a live source or adapter")
    for name in ("execute", "catalog", "capabilities", "validate_task", "validate_call"):
        monkeypatch.setattr(harness.adapter, name, source_must_not_run)
    # A new Store/Objects/Workbench with no registry simulates a fresh process.
    replacement_store = reopen_test_store(harness.store)
    try:
        reopened = Workbench(replacement_store, Objects(harness.objects.root), Registry([], []), harness.actor,
                             {harness.project: [harness.adapter.ref]}).reopen(run_id)
    finally:
        replacement_store.engine.dispose()
    assert reopened["operation"] == "replay_saved_records"
    assert reopened["source_queries_executed"] == 0
    assert reopened["frozen_rerun_available"] is False
    assert reopened["report"] == published
    assert reopened["steps"][0]["evidence"] == item
    assert reopened["steps"][0]["decision"]["args"] == {"entity": "alpha", "day": "2026-01-02"}
    assert reopened["skill"]["ref"] == harness.request.skill_ref
    assert reopened["catalog"]["contract"] == harness.adapter.data_contract
    assert "read_counter@2" in reopened["tools"]
    assert harness.adapter.execute_calls == 1


@pytest.mark.parametrize("object_kind", ["evidence", "report"])
def test_checksum_corruption_is_detected_on_reopen(harness, object_kind):
    run_id = harness.create()
    item = harness.call(run_id)
    harness.workbench.finalize(run_id, valid_report(item))
    if object_kind == "report":
        key = harness.row(run_id)["report_hash"]
    else:
        with harness.store.engine.connect() as connection:
            key = connection.execute(select(evidence.c.object_hash).where(evidence.c.id == item["evidence_id"])).scalar_one()
    path = harness.objects.root / (key + ".json")
    path.chmod(0o600)
    path.write_bytes(b'{"synthetic_corruption":true}')
    assert_error("artifact_corrupt", harness.workbench.reopen, run_id)


@pytest.mark.parametrize("binding", ["catalog", "skill", "tool"])
def test_version_changes_block_further_queries_and_finalization(harness, binding):
    run_id = harness.create()
    item = harness.call(run_id)
    if binding == "catalog":
        harness.adapter.catalog_revision += 1
    elif binding == "skill":
        skill = harness.registry.skills[harness.request.skill_ref]
        harness.registry.skills[skill.ref] = skill.model_copy(update={"instructions": "Changed method under the same version"})
    else:
        spec = harness.adapter.tools["read_counter@2"]
        harness.adapter.tools[spec.ref] = spec.model_copy(update={"description": "Changed tool under the same version"})
    assert_error("version_changed", harness.call, run_id, key="after-change")
    assert_error("version_changed", harness.workbench.finalize, run_id, valid_report(item))
    assert harness.adapter.execute_calls == 1 and harness.row(run_id)["tool_calls"] == 1
    assert harness.row(run_id)["report_hash"] is None
    assert harness.event_rows(run_id)[-1]["payload"]["code"] == "version_changed"


@pytest.mark.parametrize("scope", ["other_actor", "other_project", "source_revoked"])
def test_other_principals_or_projects_cannot_access_run_evidence_or_report(harness, scope):
    run_id = harness.create()
    item = harness.call(run_id)
    report = valid_report(item)
    actor = "different-actor" if scope == "other_actor" else harness.actor
    projects = {harness.project: [harness.adapter.ref]}
    if scope == "other_project":
        projects = {"unrelated-test-project": [harness.adapter.ref]}
    elif scope == "source_revoked":
        projects = {harness.project: []}
    stranger = Workbench(harness.store, harness.objects, harness.registry, actor, projects)
    assert_error("forbidden", stranger.reopen, run_id)
    assert_error("forbidden", stranger.read_evidence, run_id, item["evidence_id"])
    assert_error("forbidden", stranger.finalize, run_id, report)
    assert_error("forbidden", stranger.call, run_id, "read_counter@2",
                 {"entity": "alpha", "day": "2026-01-02"}, "foreign", "Forbidden scope test")
    assert harness.adapter.execute_calls == 1


def test_readonly_failure_is_persisted_and_explicit_new_action_can_continue(harness):
    run_id = harness.create()
    harness.adapter.fail_execution = True
    assert_error("tool_failed", harness.call, run_id)
    assert harness.row(run_id)["state"] == "RUNNING"
    assert harness.row(run_id)["tool_calls"] == 1
    assert_error("action_already_used", harness.call, run_id)
    replay = harness.workbench.reopen(run_id)
    assert replay["steps"][0]["state"] == "FAILED"
    assert replay["attempts"][0]["state"] == "FAILED"
    assert replay["attempts"][0]["error_object"] is not None
    assert replay["steps"][0]["evidence"] is None
    assert replay["report"] is None
    assert harness.adapter.execute_calls == 1
    harness.adapter.fail_execution = False
    item = harness.call(run_id, key="try-again")
    assert harness.adapter.execute_calls == 2
    assert harness.workbench.finalize(run_id, valid_report(item))["validation"]["status"] == "valid"
