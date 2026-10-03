"""P0 hook wiring only: synthetic exceptions are not process-kill/recovery tests."""
import pytest
from sqlalchemy import select

from analysis_agent.runtime import Workbench
from analysis_agent.storage import evidence, steps
from test_runtime import harness, real_store, valid_report  # Shared guarded fixtures.


POINTS = (
    "after_intent_commit", "after_adapter_return", "after_evidence_cas",
    "after_evidence_accept", "during_report_publish",
)


class SyntheticCrash(BaseException):
    """Bypass ordinary tool-error handling, without terminating this test process."""


def install_hook(harness, callback):
    harness.workbench = Workbench(
        harness.store, harness.objects, harness.registry, harness.actor,
        {harness.project: [harness.adapter.ref]}, fault_hook=callback,
    )


def test_passive_hook_observes_real_commit_boundaries_and_preserves_result(harness):
    seen = []

    def observe(point, context):
        if point not in POINTS:
            return  # Preserve the original five-boundary P0 contract.
        seen.append(point)
        assert set(context) <= {"run_id", "step_id", "attempt_id", "evidence_id", "object_hash"}
        # These use independent connections, so an uncommitted write is not visible.
        row = harness.row(context["run_id"])
        event_kinds = [e["kind"] for e in harness.event_rows(context["run_id"])]
        with harness.store.engine.connect() as connection:
            step = connection.execute(select(steps).where(
                steps.c.run_id == context["run_id"])).mappings().one()
            saved = connection.execute(select(evidence).where(
                evidence.c.run_id == context["run_id"])).mappings().first()
        if point in POINTS[:3]:
            assert row["state"] == "RUNNING"
            assert step["state"] == "DISPATCHED" and saved is None
            assert event_kinds == ["RUN_ADMITTED", "SKILL_DELIVERED", "TOOL_DISPATCHED"]
            assert harness.adapter.execute_calls == (0 if point == "after_intent_commit" else 1)
        if point in {"after_evidence_cas", "after_evidence_accept"}:
            assert harness.objects.get(context["object_hash"])["run_id"] == context["run_id"]
        if point == "after_evidence_accept":
            assert step["state"] == "ACCEPTED"
            assert saved["id"] == context["evidence_id"]
            assert event_kinds[-1] == "EVIDENCE_ACCEPTED"
        if point == "during_report_publish":
            assert row["state"] == "RUNNING" and row["report_hash"] is None
            assert "REPORT_PUBLISHED" not in event_kinds
            assert harness.objects.get(context["object_hash"])["validation"]["status"] == "valid"

    install_hook(harness, observe)
    run_id = harness.create()
    item = harness.call(run_id)
    published = harness.workbench.finalize(run_id, valid_report(item))
    assert seen == list(POINTS)
    assert harness.row(run_id)["state"] == "SUCCEEDED"
    assert published["report"]["facts"][0]["value"] == 12
    assert harness.workbench.reopen(run_id)["report"] == published
    assert seen == list(POINTS)  # Reopening does not invoke execution hooks.


@pytest.mark.parametrize("target", POINTS)
def test_explicit_fault_callback_can_interrupt_each_boundary(harness, target):
    seen = []

    def interrupt(point, context):
        if point not in POINTS:
            return  # New P1 points are exercised by the separate kill suite.
        seen.append(point)
        if point == target:
            raise SyntheticCrash(point)

    install_hook(harness, interrupt)
    run_id = harness.create()
    with pytest.raises(SyntheticCrash, match=target):
        item = harness.call(run_id)
        harness.workbench.finalize(run_id, valid_report(item))
    assert seen == list(POINTS[:POINTS.index(target) + 1])
    # This establishes reachability/propagation only. Recovery belongs to P1+.
