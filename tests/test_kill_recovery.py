"""Ten finite SIGKILL repetitions at each P1 hook; R04 remains NOT RUN."""
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import uuid

import pytest

from analysis_agent.contracts import WorkbenchError
from kill_scenario import (
    AFTER_ACTION, CREATE_KEY, POINTS, build_scenario, cleanup_project, snapshot, valid_report,
)


WORKER = Path(__file__).with_name("kill_worker.py")
PUBLIC_ROOT = Path(__file__).resolve().parents[1]
ACCEPTED_POINTS = {"after_evidence_accept", "during_report_publish"}
DISPATCHED_POINTS = set(POINTS) - ACCEPTED_POINTS - {
    "create_after_cas_before_insert", "call_after_decision_cas",
}
ORPHAN_POINTS = {"call_after_decision_cas", "after_evidence_cas", "accept_before_commit",
                 "during_report_publish", "unknown_mark_before_commit", "failed_mark_before_commit"}


def expected_crash_state(point):
    if point == "create_after_cas_before_insert":
        return {"run_state": None, "attempt_state": None, "accepted_evidence": 0, "published_reports": 0}
    if point == "call_after_decision_cas":
        return {"run_state": "ADMITTED", "attempt_state": None, "accepted_evidence": 0, "published_reports": 0}
    return {"run_state": "RUNNING", "attempt_state": "ACCEPTED" if point in ACCEPTED_POINTS else "DISPATCHED",
            "accepted_evidence": 1 if point in ACCEPTED_POINTS else 0, "published_reports": 0}


def assert_crash_state(point, marker, observed):
    expected = expected_crash_state(point)
    assert len(observed["runs"]) == (0 if expected["run_state"] is None else 1)
    assert len(observed["attempts"]) == (0 if expected["attempt_state"] is None else 1)
    assert len(observed["steps"]) == len(observed["attempts"])
    assert len(observed["evidence"]) == expected["accepted_evidence"]
    assert observed["resolutions"] == []
    kinds = [event["kind"] for event in observed["events"]]
    assert "REPORT_PUBLISHED" not in kinds
    assert "TOOL_FAILED" not in kinds and "TOOL_OUTCOME_UNCERTAIN" not in kinds
    if expected["run_state"] is None:
        assert observed["events"] == [] and observed["release_count"] == 0
        assert len(observed["objects"]) == 4
        assert all(not item["referenced_by_record"] for item in observed["objects"])
        return
    run = observed["runs"][0]
    assert run["id"] == marker["context"]["run_id"]
    assert run["state"] == expected["run_state"]
    assert run["report_hash"] is None and run["report_attempts"] == 0
    assert run["tool_calls"] == (0 if expected["attempt_state"] is None else 1)
    if expected["attempt_state"] is not None:
        assert observed["attempts"][0]["state"] == expected["attempt_state"]
        assert observed["steps"][0]["state"] == expected["attempt_state"]
        assert observed["attempts"][0]["id"] == observed["steps"][0]["attempt_id"]
    if point in DISPATCHED_POINTS:
        assert kinds == ["RUN_ADMITTED", "SKILL_DELIVERED", "TOOL_DISPATCHED"]
        assert observed["attempts"][0]["evidence_id"] is None
        assert observed["attempts"][0]["error_object"] is None
    elif point in ACCEPTED_POINTS:
        assert kinds == ["RUN_ADMITTED", "SKILL_DELIVERED", "TOOL_DISPATCHED", "EVIDENCE_ACCEPTED"]
    else:
        assert kinds == ["RUN_ADMITTED", "SKILL_DELIVERED"]
    if point in ORPHAN_POINTS:
        object_hash = marker["context"]["object_hash"]
        orphan = next(item for item in observed["objects"] if item["hash"] == object_hash)
        assert orphan["referenced_by_record"] is False


def recover(scenario, config, before, record):
    point = config["point"]
    if point == "create_after_cas_before_insert":
        created = scenario.workbench.create(scenario.request, CREATE_KEY)
        assert created["created"] is True
        run_id = created["run_id"]
        record["recovery_route"].append("recreate_same_request_key")
        item = scenario.call(run_id, AFTER_ACTION)
        record["recovery_route"].append("new_action_accepted")
    else:
        run_id = before["runs"][0]["id"]
        reopened = scenario.workbench.reopen(run_id)
        assert reopened["source_queries_executed"] == 0
        record["recovery_route"].append("fresh_workbench_reopen")
        if point in DISPATCHED_POINTS:
            attempt_id = before["attempts"][0]["id"]
            reconciliation = scenario.workbench.reconcile(run_id, older_than_seconds=0)
            assert reconciliation["reconciled"] == [attempt_id]
            assert reconciliation["state"] == "WAITING_RECONCILIATION"
            reconciled = scenario.workbench.reopen(run_id)
            assert reconciled["attempts"][0]["state"] == "UNKNOWN"
            record["recovery_route"].append("explicit_reconcile_to_UNKNOWN")
            resolution = scenario.workbench.resolve(
                run_id, attempt_id, actor="synthetic-p1-operator",
                reason="The pinned synthetic counter is side_effects=none; classify the killed dispatch as failed.",
            )
            assert resolution["attempt_state"] == "RESOLVED_FAILED" and resolution["state"] == "RUNNING"
            resolved = scenario.workbench.reopen(run_id)
            assert resolved["resolutions"][0]["actor"] == "synthetic-p1-operator"
            assert resolved["resolutions"][0]["reason"].startswith("The pinned synthetic counter")
            record["recovery_route"].append("named_resolution_to_RESOLVED_FAILED")
            item = scenario.call(run_id, AFTER_ACTION)
            record["recovery_route"].append("new_action_accepted")
        elif point in ACCEPTED_POINTS:
            item = scenario.workbench.read_evidence(run_id, before["evidence"][0]["id"])
            assert scenario.adapter.execute_calls == 0
            record["recovery_route"].append("reuse_durably_accepted_evidence")
        else:
            item = scenario.call(run_id, AFTER_ACTION)
            record["recovery_route"].append("new_action_accepted")
    report = valid_report(item)
    published = scenario.workbench.finalize(run_id, report)
    assert published["validation"]["status"] == "valid"
    record["recovery_route"].append("publish_report_once")
    # A repeated terminal submission must neither append events nor publish again.
    events_before = scenario.workbench.reopen(run_id)["events"]
    with pytest.raises(WorkbenchError) as caught:
        scenario.workbench.finalize(run_id, report)
    assert caught.value.code == "invalid_state"
    assert scenario.workbench.reopen(run_id)["events"] == events_before
    return run_id


@pytest.mark.parametrize("point", POINTS)
@pytest.mark.parametrize("delay_ms", range(10))
def test_sigkill_boundary_recovery(point, delay_ms, tmp_path):
    url_file = os.environ.get("WORKBENCH_TEST_DB_URL_FILE")
    if not url_file:
        pytest.skip("SIGKILL integration requires the approved independent PostgreSQL instance")
    evidence_dir = Path(os.environ.get("WORKBENCH_KILL_EVIDENCE_DIR", str(PUBLIC_ROOT / "docs/evidence/BATCH1/kill-raw")))
    evidence_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    unique = uuid.uuid4().hex
    config = {
        "url_file": url_file, "objects": str(tmp_path / "objects"),
        "project": "p1-kill-" + unique, "actor": "synthetic-actor-" + unique,
        "point": point, "delay_ms": delay_ms, "marker": str(tmp_path / "hook-marker.json"),
    }
    config_path = tmp_path / "synthetic-case.json"
    config_path.write_text(json.dumps(config), encoding="utf-8")
    config_path.chmod(0o600)
    raw_path = evidence_dir / f"{point}-{delay_ms:02d}-{unique}.json"
    record = {"schema": "p1_sigkill_probe@1", "point": point, "delay_ms": delay_ms,
              "case_id": unique, "project": config["project"], "result": "FAILED",
              "acceptance_id": "R04", "full_acceptance_status": "NOT RUN",
              "expected_crash_state": expected_crash_state(point), "recovery_route": []}
    scenario = None
    try:
        child_env = dict(os.environ)
        child_env["PYTHONPATH"] = os.pathsep.join(
            [str(PUBLIC_ROOT / "src"), str(Path(__file__).parent), child_env.get("PYTHONPATH", "")])
        child = subprocess.run([sys.executable, str(WORKER), "--case-file", str(config_path)],
                               env=child_env, capture_output=True, text=True, timeout=30)
        record["child_returncode"] = child.returncode
        record["child_stdout"] = child.stdout
        record["child_stderr"] = child.stderr
        assert child.returncode == -signal.SIGKILL
        marker = json.loads(Path(config["marker"]).read_text(encoding="utf-8"))
        record["marker"] = marker
        assert marker["point"] == point and marker["delay_ms"] == delay_ms
        assert marker["marker_fsynced_before_kill"] is True
        # No child Store/Workbench instance is reused in the parent.
        scenario = build_scenario(config)
        before = snapshot(scenario, config["project"])
        record["observed_after_crash"] = before
        assert_crash_state(point, marker, before)
        run_id = recover(scenario, config, before, record)
        scenario.store.engine.dispose()
        scenario = build_scenario(config)  # Independent final persisted-record check.
        reopened = scenario.workbench.reopen(run_id)
        assert reopened["run"]["state"] == "SUCCEEDED"
        assert reopened["report"]["validation"]["status"] == "valid"
        assert reopened["report"]["report"]["facts"][0]["value"] == 12
        assert [item["seq"] for item in reopened["events"]] == list(range(1, len(reopened["events"]) + 1))
        published_count = sum(event["kind"] == "REPORT_PUBLISHED" for event in reopened["events"])
        assert published_count == 1
        assert all(attempt["state"] not in {"DISPATCHED", "UNKNOWN"} for attempt in reopened["attempts"])
        record["final"] = {"run_id": run_id, "state": reopened["run"]["state"],
                           "attempt_states": [attempt["state"] for attempt in reopened["attempts"]],
                           "event_count": len(reopened["events"]), "published_count": published_count,
                           "resolution_count": len(reopened["resolutions"]),
                           "fresh_reopen_source_queries": reopened["source_queries_executed"]}
        record["result"] = "PASSED"
    except BaseException as exc:
        record["error_type"] = type(exc).__name__
        raise
    finally:
        try:
            if scenario is None:
                scenario = build_scenario(config)
            record["cleanup"] = cleanup_project(scenario, config["project"])
        except BaseException as exc:
            record["result"] = "FAILED"
            record["cleanup_error_type"] = type(exc).__name__
            raise
        finally:
            try:
                if scenario is not None:
                    scenario.store.engine.dispose()
                if Path(config["objects"]).exists():
                    shutil.rmtree(config["objects"])
                config_path.unlink(missing_ok=True)
                Path(config["marker"]).unlink(missing_ok=True)
                record["temporary_objects_removed"] = not Path(config["objects"]).exists()
            except BaseException as exc:
                record["result"] = "FAILED"
                record["temporary_cleanup_error_type"] = type(exc).__name__
                raise
            finally:
                raw_path.write_text(json.dumps(record, indent=2, sort_keys=True) + "\n", encoding="utf-8")
                raw_path.chmod(0o600)
