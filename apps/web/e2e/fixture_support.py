"""Public synthetic fixtures; no provider, private objects or production DB."""
from pathlib import Path
import uuid

from analysis_agent.api import create_app
from analysis_agent.api_ui import router
from analysis_agent.contracts import FactV2, EvidenceInput, Registry, Report, RunRequest, ResultSpec, RequiredFact
from analysis_agent.demo import CommerceAdapter, demo_skill, default_parameters
from analysis_agent.runtime import Workbench
from analysis_agent.storage import Objects

ATTACK = '<img src=x onerror="globalThis.p9aInjected=true"><script>globalThis.p9aInjected=true</script>'


def make_workbench(store, root: Path, project="p9a-public"):
    adapter = CommerceAdapter("normal")
    Objects.initialize(root, "public")
    wb = Workbench(store, Objects(root), Registry([adapter], [demo_skill()]),
                   "p9a-synthetic", {project: [adapter.ref]})
    return wb


def request_for(wb, project=None, **changes):
    project = project or next(iter(wb.projects))
    values = dict(project_id=project, source_ref=wb.projects[project][0], skill_ref=demo_skill().ref,
                  question="公开合成订单查询", parameters=default_parameters(),
                  result_spec=ResultSpec(requirements=[RequiredFact(column="orders", entity={"window": "current"})]))
    values.update(changes)
    return RunRequest(**values)


def publish(wb, *, column="orders", title="公开合成订单观察", driver="policy_fixture", blocked=False):
    rid = wb.create(request_for(wb), str(uuid.uuid4()), driver=driver)["run_id"]
    evidence = wb.call(rid, "compare@2", {}, "compare", "只读取公开合成订单")
    time = {"start": default_parameters()["current_start"], "end": default_parameters()["current_end"]}
    # The adapter serializes UTC as Z; input has the same interval spelling.
    fact = FactV2(inputs=[EvidenceInput(evidence_id=evidence["evidence_id"], row=0, column=column,
                                      entity={"window": "current"}, time_range=time)],
                  value=999 if blocked else evidence["rows"][0][column], unit=evidence["units"][column])
    artifact = wb.finalize(rid, Report(title=title, result_status="complete" if column == "orders" else "partial",
                                      facts=[fact], hypotheses=[ATTACK] if title == ATTACK else []))
    expected = "blocked" if blocked else "warning" if title == ATTACK or column != "orders" else "valid"
    assert artifact["validation"]["status"] == expected, artifact["validation"]
    return rid


def seed(wb):
    ids = {"report": publish(wb, title=ATTACK), "compatible": publish(wb),
           "incompatible": publish(wb, column="order_amount"), "blocked": publish(wb, blocked=True)}
    rid = wb.create(request_for(wb), str(uuid.uuid4()), driver="policy_fixture")["run_id"]
    def interrupt(point, identifiers):
        if point == "after_intent_commit":
            raise KeyboardInterrupt("Synthetic dispatch interruption")
    wb._fault_hook = interrupt
    try:
        wb.call(rid, "compare@2", {}, "interrupted", "合成中断")
    except KeyboardInterrupt:
        pass
    finally:
        wb._fault_hook = None
    attempt = wb.reconcile(rid, older_than_seconds=0)["reconciled"][0]
    wb.resolve(rid, attempt, actor="p9a-fixture-operator", reason="合成只读工具未执行，无外部副作用")
    ids["execution"] = rid
    return ids


def app_for(wb, token):
    app = create_app(wb, token)
    app.state.workbench = wb
    app.include_router(router)
    return app
