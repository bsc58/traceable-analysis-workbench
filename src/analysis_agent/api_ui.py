"""Read-only UI projection. Host owns authentication, principal and mounting.

No model calls, migrations, adapter execution, or operator mutations. Host sets
app.state.workbench and includes router inside its authenticated FastAPI app.
"""
from copy import deepcopy

from fastapi import APIRouter, Query, Request
from sqlalchemy import select

from .contracts import RunRequest, WorkbenchError, canonical, digest
from .storage import attempts, evidence, events, resolutions, steps

router = APIRouter(prefix="/ui", tags=["workbench-ui"])


def _wb(request):
    wb = getattr(request.app.state, "workbench", None)
    if wb is None:
        raise WorkbenchError("ui_not_configured", "Host has not attached its Workbench", 503)
    return wb


def _admit_read(wb, connection, run_id):
    # Runtime's P3 cutoff-refusal path writes audit events. This router must stay
    # read-only even on refusal; use the same cutoff predicate without writing.
    row = wb._run(connection, run_id, apply_cutoff=False)
    if wb._cross_run_blocker(connection, row) is not None:
        raise WorkbenchError("as_of_guard", "Run exceeds the active knowledge cutoff", 403)
    return row


def _record(wb, run_id):
    # Reconstruct saved objects with SELECTs only, including refusal paths.
    with wb.store.engine.connect() as connection:
        run = _admit_read(wb, connection, run_id)
        def rows(table, order):
            return [dict(row) for row in connection.execute(select(table).where(
                table.c.run_id == run_id).order_by(*order)).mappings()]
        step_rows = rows(steps, [steps.c.created_at])
        attempt_rows = rows(attempts, [attempts.c.started_at, attempts.c.seq])
        resolution_rows = rows(resolutions, [resolutions.c.created_at])
        evidence_rows = {row["id"]: row for row in rows(evidence, [evidence.c.created_at])}
        blocked = connection.execute(select(events.c.payload).where(
            events.c.run_id == run_id, events.c.kind == "REPORT_BLOCKED").order_by(
                events.c.seq.desc()).limit(1)).scalar_one_or_none()
    manifest = run["manifest"]
    for step in step_rows:
        step["decision"] = wb.objects.get(step["decision_hash"])
        saved = evidence_rows.get(step["evidence_id"])
        step["evidence"] = wb.objects.get(saved["object_hash"]) if saved else None
    for attempt in attempt_rows:
        attempt["error"] = wb.objects.get(attempt["error_object"]) if attempt["error_object"] else None
    report_hash = run["report_hash"] or (blocked or {}).get("candidate_object")
    return {"run": run, "steps": step_rows, "attempts": attempt_rows, "resolutions": resolution_rows,
            "skill": {"ref": manifest["request"]["skill_ref"], "digest": manifest["bindings"]["skill"]},
            "catalog": wb.objects.get(manifest["catalog_object"]),
            "tools": wb.objects.get(manifest["tools_object"]),
            "report": wb.objects.get(report_hash) if report_hash else None,
            "report_kind": "published" if run["report_hash"] else "blocked_candidate" if report_hash else "absent",
            "operation": "replay_saved_records", "source_queries_executed": 0}


def _versions(record):
    manifest = record["run"]["manifest"]
    bindings = manifest.get("bindings", {})
    return {key: bindings.get(key, "未核实") for key in ("skill", "catalog", "code", "environment")}


def _checked_facts(record):
    artifact = record.get("report") or {}
    validation = artifact.get("validation", {})
    # Legacy free labels are deliberately not projected as checked content.
    facts = validation.get("normalized_facts")
    if facts is None:
        return []
    checked = validation.get("verified_fact_indices", [])
    return [{k: deepcopy(v) for k, v in fact.items() if k not in {"note", "label"}}
            for fact in facts if fact.get("fact") in checked]


def _fact_key(fact):
    inputs = fact.get("inputs") or []
    if not inputs or any(not item.get("entity") or not item.get("column") or
                         not item.get("unit") or not item.get("time") for item in inputs):
        raise WorkbenchError("incompatible_facts", "实体键、列、单位或时间信息缺失，拒绝比较", 422)
    return canonical({"operation": fact.get("operation"), "unit": fact.get("unit"),
                      "inputs": [{k: item[k] for k in ("column", "entity", "unit", "time")}
                                 for item in inputs]})


def compare_records(left, right):
    """Conservative exact semantic match; never pair by array index or free label."""
    versions = {"left": _versions(left), "right": _versions(right)}
    versions["changed"] = [key for key in versions["left"]
                           if versions["left"][key] != versions["right"][key]]
    def index(record):
        result = {}
        if (record.get("report") or {}).get("validation", {}).get("status") not in {"valid", "warning"}:
            raise WorkbenchError("incompatible_facts", "报告没有可用于比较的已接纳校验结果", 422)
        for fact in _checked_facts(record):
            key = _fact_key(fact)
            if key in result:
                raise WorkbenchError("incompatible_facts", "事实键重复，无法唯一配对", 422)
            result[key] = fact
        return result
    try:
        lhs, rhs = index(left), index(right)
        if not lhs or set(lhs) != set(rhs):
            raise WorkbenchError("incompatible_facts", "实体键、列、单位、时间或运算不兼容，拒绝比较", 422)
    except WorkbenchError as exc:
        return {"status": "UNSUPPORTED", "error": {"code": exc.code, "message": exc.message},
                "versions": versions, "facts": []}
    return {"status": "PASSED", "scope": "结构化事实兼容性；不校验文字或因果解释", "versions": versions,
            "facts": [{"identity": lhs[key]["inputs"], "operation": lhs[key]["operation"],
                       "unit": lhs[key]["unit"], "left": lhs[key]["value"], "right": rhs[key]["value"]}
                      for key in sorted(lhs)]}


@router.get("/projects")
def projects(request: Request):
    return {"projects": sorted(_wb(request).projects)}


@router.get("/projects/{project_id}/configuration")
def configuration(project_id: str, request: Request):
    wb = _wb(request)
    wb._authorize(project_id)
    sources = []
    for ref in wb.projects[project_id]:
        wb._authorize(project_id, ref)
        adapter = wb.registry.adapters[ref]
        catalog = adapter.catalog()
        capabilities = adapter.capabilities()
        sources.append({"ref": ref, "status": "未核实", "status_reason": "已注册；未执行数据源连通性探针",
                        "catalog": catalog, "catalog_digest": digest(catalog),
                        "capabilities": {key: {"status": "PASSED" if value is True else "UNSUPPORTED",
                            "reason": "数据源声明支持" if value is True else "数据源声明不支持此能力"}
                            for key, value in capabilities.items()},
                        "tools": [tool.model_dump(mode="json") for tool in adapter.tools.values()],
                        "skills": [{**skill.model_dump(mode="json"), "digest": digest(skill.model_dump(mode="json"))}
                                   for skill in wb.registry.skills.values()
                                   if skill.privacy == adapter.privacy and skill.data_contract == adapter.data_contract
                                   and all(tool in adapter.tools for tool in skill.tools)]})
    schema = RunRequest.model_json_schema()["properties"]
    budgets = {key: {"minimum": schema[key]["minimum"], "maximum": schema[key]["maximum"],
                     "default": schema[key]["default"]} for key in ("tool_budget", "report_budget")}
    return {"project_id": project_id, "sources": sources, "budgets": budgets,
            "scope": "本机固定身份；配置只读；模型预算尚未接入"}


@router.get("/runs/{run_id}/events")
def incremental_events(run_id: str, request: Request, after: int = Query(0, ge=0),
                       limit: int = Query(100, ge=1, le=200)):
    wb = _wb(request)
    with wb.store.engine.connect() as connection:
        run = _admit_read(wb, connection, run_id)
        rows = [dict(row) for row in connection.execute(select(events).where(
            events.c.run_id == run_id, events.c.seq > after).order_by(events.c.seq).limit(limit + 1)).mappings()]
    page = rows[:limit]
    return {"events": page, "next_cursor": page[-1]["seq"] if page else after,
            "has_more": len(rows) > limit, "state": run["state"]}


@router.get("/runs/{run_id}/detail")
def detail(run_id: str, request: Request):
    record = _record(_wb(request), run_id)
    record.pop("events", None)  # Events are fetched only through the bounded cursor endpoint.
    record["checked_facts"] = _checked_facts(record)
    return record


@router.get("/projects/{project_id}/compare")
def compare(project_id: str, request: Request, left: str, right: str):
    wb = _wb(request)
    wb._authorize(project_id)
    records = [_record(wb, run_id) for run_id in (left, right)]
    if any(record["run"]["project_id"] != project_id for record in records):
        raise WorkbenchError("forbidden", "Both runs must belong to this project", 403)
    return compare_records(*records)


@router.get("/projects/{project_id}/evaluations")
def evaluations(project_id: str, request: Request):
    wb = _wb(request)
    # No persistent model-evaluation repository exists in B0. Do not invent one
    # from successful runs. Only display actually stored fixture runs as activity.
    fixture_runs = []
    for row in wb.history(project_id):
        record = _record(wb, row["id"])
        execution = record["run"]["manifest"].get("execution", {})
        if execution.get("driver") == "policy_fixture":
            fixture_runs.append({"run_id": row["id"], "state": row["state"],
                                 "label": "夹具，非模型评测", "created_at": row["created_at"]})
    return {"status": "NOT RUN", "records": [], "fixture_runs": fixture_runs,
            "reason": "尚未接入持久化模型评测记录；运行成功不构成模型评测", "scores": None}
