"""P7 synthetic probes; reference answers live outside the public source tree."""
import ast
from copy import deepcopy
import importlib.util
import json
import os
from pathlib import Path
import re
import uuid

import pytest
from sqlalchemy import select

from analysis_agent.contracts import FactV2, Registry, Report, RunRequest, WorkbenchError
from analysis_agent.runtime import Workbench
from analysis_agent.scenarios import build_registry
from analysis_agent.scenarios import service_ops, data_quality
from analysis_agent.storage import Objects, attempts, events
from analysis_agent.validation import validate_report
from database_guard import open_test_store

ROOT = Path(__file__).resolve().parents[1]
SCENARIOS = {"service_ops": service_ops, "data_quality": data_quality}


def load_module(path):
    spec = importlib.util.spec_from_file_location("p7_offline_module", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def family(name):
    return json.loads((ROOT / "eval" / name / "task_family.json").read_text())


def adapter(name, data=None):
    cls = service_ops.ServiceOpsAdapter if name == "service_ops" else data_quality.DataQualityAdapter
    return cls(data)


@pytest.fixture(scope="module")
def references():
    path = Path(os.environ.get("P7_REFERENCE_DIR", ROOT.parent.parent / "work/eval_private/p7"))
    if not path.is_dir():
        pytest.skip("P7 owner-controlled reference directory is required; no answers are distributed")
    assert path.stat().st_mode & 0o777 == 0o700
    assert not path.resolve().is_relative_to(ROOT)
    return path


@pytest.fixture(scope="module")
def p7_store():
    filename = os.environ.get("WORKBENCH_TEST_DB_URL_FILE")
    if not filename:
        pytest.skip("P7 isolated PostgreSQL instance required")
    store = open_test_store(filename)
    assert store._approved_test_database in {"workbench_test_b1p7", "workbench_test_b2e", "workbench_test_b2r"}
    store.check_version()
    yield store
    store.engine.dispose()


def case(references, name, index):
    return json.loads((references / (name + "_cases.json")).read_text())[index]


def setup_run(store, tmp_path, name, data):
    source = adapter(name, data)
    skill = SCENARIOS[name].public_skill()
    project = "p7-" + uuid.uuid4().hex
    bench = Workbench(store, Objects(tmp_path / "objects"), Registry([source], [skill]), "p7-fixture", {project: [source.ref]})
    task = family(name)
    request = RunRequest(project_id=project, source_ref=source.ref, skill_ref=skill.ref,
                         question=task["question"], parameters=task["parameters"], result_spec=task["result_spec"])
    run = bench.create(request, "create-" + uuid.uuid4().hex, driver="policy_fixture")["run_id"]
    return source, bench, run


def observation(item, row_index, column, value, name):
    row = item["rows"][row_index]
    time_range = ({"start": item["time_range"][row["window"] + "_start"],
                   "end": item["time_range"][row["window"] + "_end"]}
                  if name == "service_ops" else item["time_range"])
    return FactV2(inputs=[{"evidence_id": item["evidence_id"], "row": row_index, "column": column,
                          "entity": {k: row[k] for k in item["entity_keys"]}, "time_range": time_range}],
                  value=value, unit=item["units"][column])


@pytest.mark.parametrize("name", SCENARIOS)
def test_registry_assets_and_contract(name):
    registry = build_registry(name)
    source = next(iter(registry.adapters.values()))
    skill = next(iter(registry.skills.values()))
    generated = load_module(ROOT / "examples" / name / "generate.py").generate()
    assert source._data == generated
    assert skill.instructions == (ROOT / "src/analysis_agent/scenarios" / name / "SKILL.md").read_text()
    assert source.ref == adapter(name, generated).ref
    assert re.fullmatch(r"src_[0-9a-f]{24}@1", source.ref)
    catalog = source.catalog()
    for field in ("fields", "time_fields", "entity_keys", "grain", "units", "metrics", "reference_time", "derivation_policy"):
        assert catalog[field]
    assert all(t.side_effects == "none" and t.retry == "new_action_allowed" for t in source.tools.values())
    assert "Correlation is not causation" in service_ops.public_skill().instructions
    assert all("Correlation is not causation" in t.description for t in service_ops.ServiceOpsAdapter().tools.values())


def test_unknown_registry_name_rejected():
    with pytest.raises(WorkbenchError, match="Choose"):
        build_registry("../../arbitrary.py")


@pytest.mark.parametrize("name", SCENARIOS)
@pytest.mark.parametrize("index", range(5))
def test_independent_solver_and_manual_reference(references, name, index):
    c = case(references, name, index)
    solver = load_module(references / ("service_solver.py" if name == "service_ops" else "quality_solver.py"))
    assert solver.solve(c["data"], c["parameters"]) == c["manual"]
    assert load_module(ROOT / "examples" / name / "generate.py").generate(index) == c["data"]
    source, task = adapter(name, c["data"]), family(name)
    if c["manual"]["overview_error"]:
        with pytest.raises(WorkbenchError) as exc:
            source.execute(task["first_query"], {}, c["parameters"])
        assert exc.value.code == c["manual"]["overview_error"]
    else:
        assert source.execute(task["first_query"], {}, c["parameters"]).rows == c["manual"]["overview"]
    if name == "data_quality":
        assert source.execute(task["followup_query"], {}, c["parameters"]).rows == c["manual"]["dependencies"]


def test_solver_and_generator_static_independence(references):
    # An explicit import allowlist catches aliases and indirect project imports.
    paths = list(references.glob("*solver.py")) + [ROOT / "examples" / n / "generate.py" for n in SCENARIOS]
    assert len(paths) == 4
    for path in paths:
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                assert all(n.name in {"json", "datetime", "fractions", "decimal"} for n in node.names)
            if isinstance(node, ast.ImportFrom):
                assert node.level == 0 and node.module in {"json", "datetime", "fractions", "decimal"}
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                assert node.func.id not in {"__import__", "eval", "exec", "open", "compile"}
    for path in (ROOT / "src/analysis_agent/scenarios").rglob("*.py"):
        assert not any(s in path.read_text() for s in ("eval_private", "solver", "generate.py"))


@pytest.mark.parametrize("name", SCENARIOS)
@pytest.mark.parametrize("pair", [(0, 1), (2, 3)])
def test_blind_counterfactual_only_first_semantic_result_changes(references, name, pair):
    a, b = [adapter(name, case(references, name, i)["data"]) for i in pair]
    task = family(name)
    assert a.ref != b.ref
    assert a.tools == b.tools
    assert a.execute(task["first_query"], {}, task["parameters"]).rows != b.execute(task["first_query"], {}, task["parameters"]).rows
    follow_a = a.execute(task["followup_query"], {}, task["parameters"]).model_dump()
    follow_b = b.execute(task["followup_query"], {}, task["parameters"]).model_dump()
    follow_a.pop("provenance"); follow_b.pop("provenance")
    assert follow_a == follow_b
    for source in (a, b):
        text = json.dumps([source.catalog(), source.execute(task["first_query"], {}, task["parameters"]).model_dump()])
        assert not any(word in text for word in ("case_0", "fixture_variant", "counterfactual", "generator_selection"))


@pytest.mark.parametrize("name", SCENARIOS)
def test_join_fanout_does_not_multiply_counts(name):
    source = adapter(name)
    task = family(name)
    before = source.execute(task["first_query"], {}, task["parameters"])
    data = deepcopy(source._data)
    table, key = ("errors", "error_id") if name == "service_ops" else ("checks", "check_id")
    # Both duplicate delivery and a new one-to-many child must leave distinct counts unchanged.
    data[table] += [dict(r) for r in data[table]] + [{**r, key: "additional-" + r[key]} for r in data[table]]
    after = adapter(name, data).execute(task["first_query"], {}, task["parameters"])
    assert before.rows == after.rows


@pytest.mark.parametrize("name", SCENARIOS)
@pytest.mark.parametrize("fault", ["zero", "mixed", "conflict"])
def test_explicit_data_errors(name, fault):
    source = adapter(name); data = deepcopy(source._data); task = family(name)
    if name == "service_ops":
        if fault == "zero": data["requests"] = []
        elif fault == "mixed": data["requests"][0]["duration_unit"] = "seconds"
        else: data["errors"].append({**data["errors"][0], "request_id": "different"})
    else:
        if fault == "zero": data["checks"] = []
        elif fault == "mixed": data["batches"][0]["row_unit"] = "bytes"
        else: data["checks"].append({**data["checks"][0], "passed": False})
    with pytest.raises(WorkbenchError) as exc:
        adapter(name, data).execute(task["first_query"], {}, task["parameters"])
    assert exc.value.code == {"zero": "zero_denominator", "mixed": "incompatible_units", "conflict": "data_conflict"}[fault]


@pytest.mark.parametrize("name", SCENARIOS)
def test_equivalent_timezone_offsets_and_exclusive_cutoff(name):
    source = adapter(name); task = family(name); p = deepcopy(task["parameters"])
    expected = source.execute(task["first_query"], {}, p).rows
    if name == "service_ops":
        p.update(current_start="2026-01-01T19:00:00-05:00", current_end="2026-01-01T20:00:00-05:00")
    p["as_of"] = "2026-01-01T20:00:00-05:00"
    assert source.execute(task["first_query"], {}, p).rows == expected
    data = deepcopy(source._data)
    if name == "service_ops":
        data["requests"].append({**data["requests"][0], "request_id": "late-delivery", "available_at": task["parameters"]["as_of"]})
    else:
        data["checks"].append({**data["checks"][0], "check_id": "late-delivery", "passed": False, "available_at": p["as_of"]})
    assert adapter(name, data).execute(task["first_query"], {}, p).rows == expected


@pytest.mark.parametrize("value", [0, -1, 86401, True, 1.5, "3600"])
def test_freshness_parameter_bounds(value):
    p = data_quality.default_parameters(); p["max_age_seconds"] = value
    with pytest.raises(WorkbenchError): data_quality.DataQualityAdapter().validate_task(p)


@pytest.mark.parametrize("change", [{"baseline_end": "2026-01-02T01:00:00Z"},
                                     {"current_end": "2026-01-02T00:30:00Z"},
                                     {"as_of": "2026-01-04T00:00:00Z"},
                                     {"current_start": "2026-01-02T00:00:00"}, {"service": "outside"}])
def test_service_window_bounds(change):
    with pytest.raises(WorkbenchError):
        service_ops.ServiceOpsAdapter().validate_task({**service_ops.default_parameters(), **change})


@pytest.mark.parametrize("name", SCENARIOS)
def test_runtime_rejects_scope_before_execution(p7_store, tmp_path, name, monkeypatch):
    source, bench, run = setup_run(p7_store, tmp_path, name, adapter(name)._data)
    called = []
    monkeypatch.setattr(source, "execute", lambda *args: called.append(args))
    with pytest.raises(WorkbenchError) as exc:
        bench.call(run, family(name)["first_query"], {"as_of": "2099-01-01T00:00:00Z"}, "invalid", "scope probe")
    assert exc.value.code == "invalid_parameters" and called == []
    with p7_store.engine.connect() as connection:
        assert not connection.execute(select(attempts).where(attempts.c.run_id == run)).first()
        assert connection.execute(select(events.c.kind).where(events.c.run_id == run).order_by(events.c.seq.desc())).first()[0] == "TOOL_REJECTED"


@pytest.mark.parametrize("name", SCENARIOS)
@pytest.mark.parametrize("index", range(5))
def test_manual_report_create_call_finalize(p7_store, tmp_path, references, name, index):
    c = case(references, name, index); task = family(name)
    source, bench, run = setup_run(p7_store, tmp_path, name, c["data"])
    expected, facts = c["manual"], []
    if expected["overview_error"]:
        with pytest.raises(WorkbenchError) as exc:
            bench.call(run, task["first_query"], {}, "overview", "Read the fixed overview")
        # B0 sanitizes unlisted adapter errors to tool_failed. The adapter-level
        # exact code is tested separately; request allowlist integration from main.
        assert exc.value.code == "tool_failed"
        with p7_store.engine.connect() as connection:
            assert connection.execute(select(attempts.c.state).where(attempts.c.run_id == run)).scalar_one() == "FAILED"
    else:
        item = bench.call(run, task["first_query"], {}, "overview", "Read the fixed overview")
        # Values come from handwritten reference rows, not from observed adapter values.
        for row_index, row in enumerate(expected["overview"]):
            for column, value in row.items():
                if column not in item["entity_keys"]:
                    facts.append(observation(item, row_index, column, value, name))
        follow = bench.call(run, task["followup_query"], {}, "followup", "Inspect contextual or upstream evidence")
        for row_index, row in enumerate(expected.get("dependencies", [])):
            for column, value in row.items():
                if column not in follow["entity_keys"]:
                    facts.append(observation(follow, row_index, column, value, name))
    report = Report(title="Handwritten synthetic investigation", result_status=expected["result_status"], facts=facts,
                    limitations=[] if expected["result_status"] == "complete" else ["Available evidence cannot support a complete investigation."])
    result = bench.finalize(run, report)
    assert result["validation"]["status"] == c["expected_validation"]
    reopened = bench.reopen(run)
    assert reopened["run"]["state"] == "SUCCEEDED"
    assert reopened["run"]["manifest"]["execution"]["driver"] == "policy_fixture"
    # Store raw per-variant lifecycle evidence with the hidden references, never in the public package.
    out = references / "lifecycle"; out.mkdir(mode=0o700, exist_ok=True)
    path = out / (name + "_" + c["id"] + ".json")
    path.write_text(json.dumps(reopened, indent=2, default=str) + "\n"); path.chmod(0o600)


@pytest.mark.parametrize("name,index", [("service_ops", 2), ("service_ops", 3), ("data_quality", 4)])
def test_complete_claim_blocked_for_insufficient_scope(references, name, index):
    c = case(references, name, index); source = adapter(name, c["data"]); task = family(name)
    items, facts = {}, []
    for tool in (task["first_query"], task["followup_query"]):
        item = {**source.execute(tool, {}, c["parameters"]).model_dump(), "evidence_id": tool}; items[tool] = item
        for row_index, row in enumerate(item["rows"]):
            for column, value in row.items():
                if column not in item["entity_keys"]:
                    facts.append(observation(item, row_index, column, value, name))
    result = validate_report(Report(title="Overclaim probe", result_status="complete", facts=facts), items.__getitem__,
                             result_spec=task["result_spec"], catalog=source.catalog())
    assert result["status"] == "blocked"
    assert "complete_uses_bounded_evidence" in {e["code"] for e in result["errors"]}


def test_version_mismatch_and_partial_rule_coverage():
    source = data_quality.DataQualityAdapter(); params = data_quality.default_parameters()
    data = deepcopy(source._data)
    data["checks"] = [r for r in data["checks"] if r["record_id"] != "r0"]
    assert data_quality.DataQualityAdapter(data).execute("quality_overview@1", {}, params).completeness == "bounded"
    data = deepcopy(source._data); data["dependencies"][0]["required_version"] = "v9"
    result = data_quality.DataQualityAdapter(data).execute("quality_dependencies@1", {}, params)
    assert result.completeness == "unknown" and result.rows[0]["upstream_rows"] is None
    data = deepcopy(source._data)
    for row in data["checks"]: row["version"] = "v9"
    with pytest.raises(WorkbenchError) as exc:
        data_quality.DataQualityAdapter(data).execute("quality_overview@1", {}, params)
    assert exc.value.code == "zero_denominator"
