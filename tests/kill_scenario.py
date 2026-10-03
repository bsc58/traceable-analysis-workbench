"""Shared synthetic scenario and scoped cleanup for finite SIGKILL experiments."""
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import delete, select

from analysis_agent.contracts import Registry, RunRequest, SkillRelease
from analysis_agent.runtime import Workbench
from analysis_agent.storage import Objects, attempts, evidence, events, releases, resolutions, runs, steps
from database_guard import open_test_store
from test_runtime import TinyAdapter, valid_report


POINTS = (
    "create_after_cas_before_insert", "call_after_decision_cas",
    "after_intent_commit", "after_adapter_return", "after_evidence_cas",
    "accept_before_commit", "after_evidence_accept", "during_report_publish",
    "unknown_mark_before_commit", "failed_mark_before_commit",
)
TOOL_REF = "read_counter@2"
ARGS = {"entity": "alpha", "day": "2026-01-02"}
CREATE_KEY = "synthetic-kill-create"
BEFORE_ACTION = "before-kill-action"
AFTER_ACTION = "after-recovery-action"


@dataclass
class KillScenario:
    store: object
    objects: Objects
    adapter: TinyAdapter
    workbench: Workbench
    request: RunRequest

    def call(self, run_id, action_key):
        return self.workbench.call(run_id, TOOL_REF, ARGS, action_key,
                                   "Read a synthetic counter for the crash experiment")


def build_scenario(config, *, fault_hook=None):
    store = open_test_store(config["url_file"])
    try:
        store.check_version()
        adapter = TinyAdapter()
        skill = SkillRelease(
            ref="kill_recovery_fixture@2", instructions="Read the independent synthetic counter.",
            data_contract=adapter.data_contract, tools=(TOOL_REF,),
        )
        registry = Registry([adapter], [skill])
        objects = Objects(Path(config["objects"]))
        workbench = Workbench(store, objects, registry, config["actor"],
            {config["project"]: [adapter.ref]}, fault_hook=fault_hook)
        request = RunRequest(
            project_id=config["project"], source_ref=adapter.ref, skill_ref=skill.ref,
            question="Observe the synthetic counter after explicit crash recovery.",
            parameters={"first_day": "2026-01-02", "last_day": "2026-01-03", "entities": ["alpha"]},
            result_spec={"requirements": [{"column": "requests"}]}, tool_budget=3, report_budget=3,
        )
        return KillScenario(store, objects, adapter, workbench, request)
    except BaseException:
        store.engine.dispose()
        raise


def snapshot(scenario, project):
    """Keep synthetic states and object identifiers, never connection information."""
    with scenario.store.engine.connect() as connection:
        run_rows = [dict(row) for row in connection.execute(select(runs).where(
            runs.c.project_id == project)).mappings()]
        ids = [row["id"] for row in run_rows]
        grouped = {}
        for name, table in (("steps", steps), ("attempts", attempts), ("evidence", evidence),
                            ("events", events), ("resolutions", resolutions)):
            query = select(table).where(table.c.run_id.in_(ids))
            if name == "events":
                query = query.order_by(events.c.seq)
            grouped[name] = [dict(row) for row in connection.execute(query).mappings()] if ids else []
        release_rows = [dict(row) for row in connection.execute(select(releases).where(
            releases.c.namespace.in_([project + suffix for suffix in (":source", ":skill", ":tool")]))).mappings()]
    references = set()
    for row in run_rows:
        references.update(row["manifest"][key] for key in ("skill_object", "catalog_object", "tools_object", "environment_object"))
        if row["report_hash"]:
            references.add(row["report_hash"])
    for row in grouped["steps"]:
        references.add(row["decision_hash"])
    for row in grouped["evidence"]:
        references.add(row["object_hash"])
    for row in grouped["attempts"]:
        if row["error_object"]:
            references.add(row["error_object"])
    for event in grouped["events"]:
        for key in ("candidate_object", "result_object", "error_object", "report_object", "object_hash", "decision_object"):
            value = event["payload"].get(key)
            if isinstance(value, str):
                references.add(value)
    objects = []
    for path in sorted(scenario.objects.root.glob("*.json")):
        data = scenario.objects.get(path.stem)
        objects.append({"hash": path.stem, "bytes": path.stat().st_size,
                        "schema_version": data.get("schema_version"),
                        "referenced_by_record": path.stem in references})
    return {
        "runs": [{key: row[key] for key in ("id", "state", "tool_calls", "report_attempts", "report_hash", "seq")}
                 for row in run_rows],
        **grouped, "release_count": len(release_rows), "objects": objects,
    }


def cleanup_project(scenario, project):
    """All cyclic FKs are deferred; only this unique project's rows are removed."""
    with scenario.store.engine.begin() as connection:
        ids = list(connection.execute(select(runs.c.id).where(runs.c.project_id == project)).scalars())
        if ids:
            for table in (resolutions, attempts, evidence, steps, events):
                connection.execute(delete(table).where(table.c.run_id.in_(ids)))
            connection.execute(delete(runs).where(runs.c.id.in_(ids)))
        connection.execute(delete(releases).where(releases.c.namespace.in_(
            [project + suffix for suffix in (":source", ":skill", ":tool")])))
    with scenario.store.engine.connect() as connection:
        remaining_runs = list(connection.execute(select(runs.c.id).where(runs.c.project_id == project)).scalars())
        remaining_releases = list(connection.execute(select(releases.c.ref).where(
            releases.c.namespace.in_([project + suffix for suffix in (":source", ":skill", ":tool")]))).scalars())
    assert remaining_runs == [] and remaining_releases == []
    return {"project_rows_removed": True, "release_rows_removed": True}
