"""P9a checks: actual runtime refusal, read-only projections, semantic comparison."""
from copy import deepcopy
import os
from pathlib import Path
import sys
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import event

from analysis_agent.api_ui import compare_records, router
from database_guard import open_test_store

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "apps/web/e2e"))
from fixture_support import make_workbench, request_for, seed, app_for


def record(**changes):
    fact = {"fact": 0, "value": 42, "unit": "orders", "operation": "cell", "note": "unverified",
            "inputs": [{"entity": {"window": "current"}, "column": "orders", "unit": "orders",
                        "time": {"start": "2025-01-08", "end": "2025-01-15"}}]}
    fact.update(changes)
    return {"run": {"manifest": {"bindings": {key: key + "-v1" for key in ("skill", "catalog", "code", "environment")}}},
            "report": {"validation": {"status": "valid", "normalized_facts": [fact], "verified_fact_indices": [0]}}}


def test_router_has_only_get_routes():
    assert all(route.methods == {"GET"} for route in router.routes)


def test_comparison_preserves_versions_and_excludes_unchecked_text():
    left, right = record(), record(value=45)
    right["run"]["manifest"]["bindings"] = {key: key + "-v2" for key in ("skill", "catalog", "code", "environment")}
    result = compare_records(left, right)
    assert result["status"] == "PASSED"
    assert result["facts"][0]["left"] == 42
    assert result["facts"][0]["right"] == 45
    assert set(result["versions"]["changed"]) == {"skill", "catalog", "code", "environment"}
    assert "unverified" not in str(result)


@pytest.mark.parametrize("mutation", ["unit", "entity", "time", "column", "operation", "duplicate", "empty", "blocked"])
def test_comparison_refuses_incompatible_or_ambiguous_facts(mutation):
    right = record()
    validation = right["report"]["validation"]
    fact = validation["normalized_facts"][0]
    if mutation in {"unit", "operation"}:
        fact[mutation] = "different"
    elif mutation in {"entity", "time", "column"}:
        fact["inputs"][0][mutation] = {"other": "key"} if mutation != "column" else "another"
    elif mutation == "duplicate":
        validation["normalized_facts"].append(deepcopy(fact))
    elif mutation == "blocked":
        validation["status"] = "blocked"
    else:
        validation["normalized_facts"] = []
    result = compare_records(record(), right)
    assert result["status"] == "UNSUPPORTED"
    assert result["error"]["code"] == "incompatible_facts"
    assert result["facts"] == [] and "versions" in result


@pytest.fixture(scope="module")
def harness(tmp_path_factory):
    filename = os.environ.get("WORKBENCH_TEST_DB_URL_FILE")
    if not filename:
        pytest.skip("P9a approved PostgreSQL instance required")
    store = open_test_store(filename)
    wb = make_workbench(store, tmp_path_factory.mktemp("p9a-objects"), "p9a-" + uuid.uuid4().hex)
    ids = seed(wb)
    token = "synthetic-test-credential-" + uuid.uuid4().hex
    client = TestClient(app_for(wb, token))
    client.headers["Authorization"] = "Bearer " + token
    yield wb, client, ids
    client.close()
    store.engine.dispose()


def test_auth_configuration_and_budget_contract(harness):
    wb, client, _ = harness
    project = next(iter(wb.projects))
    path = f"/ui/projects/{project}/configuration"
    assert client.get(path, headers={"Authorization": "wrong"}).status_code == 401
    data = client.get(path).json()
    assert data["budgets"]["tool_budget"] == {"minimum": 1, "maximum": 30, "default": 12}
    source = data["sources"][0]
    assert source["capabilities"]["frozen"]["status"] == "UNSUPPORTED"
    assert all(tool["side_effects"] == "none" for tool in source["tools"])
    assert source["skills"][0]["digest"] and source["catalog_digest"]
    assert source["status"] == "未核实"
    assert client.get('/ui/projects/forbidden/configuration').status_code == 403


def test_server_refuses_unsupported_frozen_and_cancel(harness):
    wb, client, ids = harness
    request = request_for(wb, consistency="frozen").model_dump(mode="json")
    response = client.post('/runs', json=request, headers={"Idempotency-Key": uuid.uuid4().hex})
    assert response.status_code == 422 and response.json()["error"]["code"] == "unsupported"
    response = client.post(f'/runs/{ids["execution"]}/cancel')
    assert response.status_code == 422 and response.json()["error"]["code"] == "unsupported"


def test_creation_idempotency_and_error_codes(harness):
    wb, client, _ = harness
    payload = request_for(wb).model_dump(mode="json")
    headers = {"Idempotency-Key": uuid.uuid4().hex}
    first = client.post('/runs', json=payload, headers=headers).json()
    second = client.post('/runs', json=payload, headers=headers).json()
    assert first["run_id"] == second["run_id"] and second["created"] is False
    payload["question"] = "changed"
    response = client.post('/runs', json=payload, headers=headers)
    assert response.status_code == 409 and response.json()["error"]["code"] == "idempotency_conflict"


def test_cursor_has_no_duplicates_and_rejects_bad_bounds(harness):
    _, client, ids = harness
    path = f'/ui/runs/{ids["execution"]}/events'
    cursor, seen = 0, []
    while True:
        response = client.get(path, params={"after": cursor, "limit": 2}).json()
        seen.extend(row["seq"] for row in response["events"])
        cursor = response["next_cursor"]
        if not response["has_more"]:
            break
    assert seen == sorted(set(seen)) and len(seen) > 2
    assert client.get(path, params={"after": cursor}).json()["events"] == []
    for params in ({"after": -1}, {"limit": 201}, {"limit": 0}):
        assert client.get(path, params=params).status_code == 422


def test_real_saved_report_attempt_resolution_and_comparison(harness):
    wb, client, ids = harness
    detail = client.get(f'/ui/runs/{ids["execution"]}/detail').json()
    assert detail["attempts"][0]["state"] == "RESOLVED_FAILED"
    assert detail["attempts"][0]["error"] and detail["resolutions"]
    blocked = client.get(f'/ui/runs/{ids["blocked"]}/detail').json()
    assert blocked["report_kind"] == "blocked_candidate" and blocked["report"]["validation"]["status"] == "blocked"
    path = f'/ui/projects/{next(iter(wb.projects))}/compare'
    assert client.get(path, params={"left": ids["report"], "right": ids["compatible"]}).json()["status"] == "PASSED"
    assert client.get(path, params={"left": ids["report"], "right": ids["incompatible"]}).json()["status"] == "UNSUPPORTED"


def test_evaluations_are_actual_fixtures_never_scores(harness):
    wb, client, ids = harness
    data = client.get(f'/ui/projects/{next(iter(wb.projects))}/evaluations').json()
    assert data["status"] == "NOT RUN" and data["scores"] is None and data["records"] == []
    assert ids["report"] in {row["run_id"] for row in data["fixture_runs"]}
    assert all(row["label"] == "夹具，非模型评测" for row in data["fixture_runs"])


def test_ui_routes_issue_selects_only(harness):
    wb, client, ids = harness
    statements = []
    def capture(conn, cursor, statement, parameters, context, executemany):
        statements.append(statement)
    event.listen(wb.store.engine, 'before_cursor_execute', capture)
    try:
        project = next(iter(wb.projects))
        paths = ['/ui/projects', f'/ui/projects/{project}/configuration', f'/ui/projects/{project}/evaluations',
                 f'/ui/runs/{ids["execution"]}/detail', f'/ui/runs/{ids["execution"]}/events',
                 f'/ui/projects/{project}/compare?left={ids["report"]}&right={ids["compatible"]}']
        for path in paths:
            assert client.get(path).status_code == 200
        assert statements and all(sql.lstrip().upper().startswith('SELECT') for sql in statements)
    finally:
        event.remove(wb.store.engine, 'before_cursor_execute', capture)


def test_cutoff_refusal_is_read_only_and_unchecked_facts_are_hidden(harness, monkeypatch):
    wb, client, ids = harness
    statements = []
    def capture(conn, cursor, statement, parameters, context, executemany):
        statements.append(statement)
    monkeypatch.setattr(wb, '_cross_run_blocker', lambda connection, row: {'reason': 'synthetic_cutoff'})
    event.listen(wb.store.engine, 'before_cursor_execute', capture)
    try:
        response = client.get(f'/ui/runs/{ids["report"]}/detail')
        assert response.status_code == 403 and response.json()['error']['code'] == 'as_of_guard'
        assert statements and all(sql.lstrip().upper().startswith('SELECT') for sql in statements)
    finally:
        event.remove(wb.store.engine, 'before_cursor_execute', capture)
    left = record()
    left['report']['validation']['verified_fact_indices'] = []
    assert compare_records(left, record())['status'] == 'UNSUPPORTED'
