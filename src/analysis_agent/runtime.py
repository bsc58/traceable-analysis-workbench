"""External-agent execution spine. No autonomous provider or recovery claim."""
from __future__ import annotations

import hashlib
import inspect
import math
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

import jsonschema
from sqlalchemy import insert, select, text, update

from .contracts import (Registry, Report, RunRequest, WorkbenchError, canonical,
                        digest, now)
from .storage import Objects, Store, access_refusals, model_invocations, run_execution, attempts, evidence, events, resolutions, runs, steps
from .validation import validate_report
from .environment import capture as capture_environment


def new_id(prefix):
    return prefix + "_" + uuid.uuid4().hex


def code_version(adapter):
    files = {p.name: hashlib.sha256(p.read_bytes()).hexdigest()
             for p in sorted(Path(__file__).parent.glob("*.py"))}
    module = inspect.getmodule(type(adapter))
    if module and getattr(module, "__file__", None):
        files["registered_adapter"] = hashlib.sha256(Path(module.__file__).read_bytes()).hexdigest()
    return digest(files)


class Workbench:
    def __init__(self, store: Store, objects: Objects, registry: Registry,
                 actor: str, projects: dict[str, list[str]], *,
                 readonly: bool = False, entrypoints=(), project_policies=None, legacy_sources=None,
                 fault_hook: Callable[[str, dict[str, str]], None] | None = None):
        self.store, self.objects, self.registry = store, objects, registry
        self.actor, self.projects = actor, projects
        self.readonly = readonly
        self.entrypoints = tuple(entrypoints)
        self.project_policies = project_policies or {}
        self.legacy_sources = legacy_sources or {}
        # Trusted test code must opt in explicitly; no environment/transport switch.
        # Crash callbacks must raise BaseException or kill the process. In
        # particular, after_adapter_return is inside except Exception coverage,
        # so ordinary exceptions there become FAILED/UNKNOWN tool outcomes.
        self._fault_hook = fault_hook

    def _writable(self):
        if self.readonly:
            raise WorkbenchError("legacy_readonly", "Archived records are available for reading only", 403)

    def _checkpoint(self, point: str, **identifiers: str) -> None:
        if self._fault_hook is not None:
            self._fault_hook(point, identifiers)

    def _authorize(self, project, source=None, *, read=False):
        allowed = self.projects.get(project, []) + (self.legacy_sources.get(project, []) if read else [])
        if project not in self.projects or (source and source not in allowed):
            raise WorkbenchError("forbidden", "Resource is outside the current local access scope", 403)

    def _runs_select(self):
        # The archive stays schema v2; never SELECT new lease columns there.
        cols = [col for col in runs.c if col.name not in {"owner", "epoch", "lease_expires_at"}] if self.readonly and self.store.read_version() == 2 else list(runs.c)
        return select(*cols)

    def _run(self, c, run_id, lock=False, *, apply_cutoff=True):
        query = self._runs_select().where(runs.c.id == run_id)
        row = c.execute(query.with_for_update() if lock else query).mappings().first()
        if not row:
            raise WorkbenchError("not_found", "Run not found", 404)
        self._authorize(row["project_id"], row["manifest"]["request"]["source_ref"], read=True)
        if row["actor"] != self.actor:
            raise WorkbenchError("forbidden", "Run belongs to another local principal", 403)
        if apply_cutoff:
            self._check_cross_run(c, row)
        return dict(row)

    @staticmethod
    def _cutoff(value):
        if value is None:
            return None
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            if len(value) == 10:
                parsed = parsed.replace(tzinfo=timezone.utc)
            if parsed.tzinfo is None:
                raise ValueError("Unknown timezone")
            return parsed.astimezone(timezone.utc)
        except (ValueError, TypeError, AttributeError):
            raise WorkbenchError("invalid_cutoff", "Knowledge cutoff must be an ISO date or timezone-qualified timestamp", 422) from None

    def _cross_run_blocker(self, c, row):
        if not self.project_policies.get(row["project_id"], {}).get("as_of_guard", False):
            return None
        active = list(c.execute(self._runs_select().where(runs.c.project_id == row["project_id"],
            runs.c.state.notin_(["SUCCEEDED", "FAILED", "CANCELLED"]))).mappings())
        if not active:
            return None
        infinity = datetime.max.replace(tzinfo=timezone.utc)
        first = min(active, key=lambda r: (self._cutoff(r["manifest"].get("knowledge_cutoff")) or infinity, r["created_at"], r["id"]))
        if first["id"] == row["id"]:
            return None
        cutoff = self._cutoff(row["manifest"].get("knowledge_cutoff"))
        earliest = self._cutoff(first["manifest"].get("knowledge_cutoff")) or infinity
        return first["id"] if cutoff is None or cutoff > earliest else None

    def _check_cross_run(self, c, row):
        blocker = self._cross_run_blocker(c, row)
        if blocker is None:
            return
        if not self.readonly:
            # The refusal must survive the caller's rollback, and must never
            # append an event to the later/terminal record being refused.
            with self.store.engine.begin() as audit:
                active = audit.execute(select(runs.c.state).where(runs.c.id == blocker).with_for_update()).scalar_one()
                if active not in {"SUCCEEDED", "FAILED", "CANCELLED"}:
                    previous = audit.execute(select(access_refusals).where(
                        access_refusals.c.run_id == blocker,
                        access_refusals.c.target_run_id == row["id"])).mappings().first()
                    if previous:
                        audit.execute(update(access_refusals).where(
                            access_refusals.c.run_id == blocker,
                            access_refusals.c.target_run_id == row["id"]).values(
                                count=previous["count"] + 1, last_refused_at=now()))
                    else:
                        audit.execute(insert(access_refusals).values(run_id=blocker,
                            target_run_id=row["id"], count=1, last_refused_at=now()))
                        self.store.event(audit, blocker, "CROSS_RUN_READ_REFUSED",
                            {"target_run_id": row["id"], "count": 1, "last_refused_at": now()})
        raise WorkbenchError("cross_run_read_refused", "A prior active task limits this project's visible cutoff", 403)

    def _assert_submit(self, c, run, lease):
        if run["manifest"].get("execution", {}).get("driver") != "internal_runner":
            if lease is not None:
                raise WorkbenchError("invalid_lease", "Worker lease supplied to external run", 409)
            return
        from .execution import assert_lease
        assert_lease(c, run, lease)

    def _bindings(self, adapter, skill, environment=None):
        return {**({"environment": digest(environment)} if environment is not None else {}),"skill": digest(skill.model_dump(mode="json")),
                "catalog": digest(adapter.catalog()),
                "tools": digest({k: v.model_dump(mode="json") for k, v in sorted(adapter.tools.items())}),
                "code": code_version(adapter)}

    def _current(self, run):
        self._authorize(run["project_id"], run["manifest"]["request"]["source_ref"])
        adapter, skill = self.registry.resolve(RunRequest(**run["manifest"]["request"]))
        environment = None
        if run["manifest"].get("environment_object"):
            saved = self.objects.get(run["manifest"]["environment_object"])
            try:
                environment = capture_environment(adapter, self.entrypoints,
                    package_names=saved["loaded_distributions"])
            except (OSError, LookupError, ImportError):
                raise WorkbenchError("version_changed", "Bound environment is unavailable", 409) from None
        if self._bindings(adapter, skill, environment) != run["manifest"]["bindings"]:
            raise WorkbenchError("version_changed", "Pinned implementation or release changed; create a new run", 409)
        return adapter, skill

    @staticmethod
    def _safe_error(exc, *, acceptance=False):
        descriptions = {
            "reader_identity_mismatch": "The source reader identity or grants do not match the registered scope.",
            "source_query_failed": "The registered read-only source query failed.",
            "incompatible": "The registered source or dependency is incompatible.",
            "result_too_large": "The tool result exceeded its declared size limit.",
            "version_changed": "Pinned implementation or release changed before acceptance.",
            "invalid_state": "The run no longer accepted this tool result.",
        }
        code = exc.code if isinstance(exc, WorkbenchError) and exc.code in descriptions else (
            "acceptance_rejected" if acceptance else "tool_failed")
        message = descriptions.get(code, "The tool result could not be accepted." if acceptance
                                   else "The registered tool or result persistence failed.")
        return code, message

    def _record_tool_outcome(self, run_id, step_id, attempt_id, state, code, message,
                             *, result_object=None, retry="never", lease=None):
        """Record a classified outcome separately from execution/acceptance.

        If the error object or transaction cannot be made durable, the original
        DISPATCHED intent remains available to explicit reconciliation. A late
        failure cannot overwrite a terminal run or an already accepted attempt.
        """
        classifications = {
            "FAILED": ("deterministic_failure", "TOOL_FAILED"),
            "UNKNOWN": ("uncertain", "TOOL_OUTCOME_UNCERTAIN"),
            "REJECTED_AFTER_EXECUTION": ("acceptance_rejected", "TOOL_REJECTED_AFTER_EXECUTION"),
        }
        outcome_class, event_name = classifications[state]
        try:
            with self.store.engine.begin() as c:
                run = self._run(c, run_id, lock=True)
                self._assert_submit(c, run, lease)
                attempt = c.execute(select(attempts).where(attempts.c.id == attempt_id,
                    attempts.c.run_id == run_id, attempts.c.step_id == step_id)).mappings().one()
                if attempt["state"] == "ACCEPTED":
                    return {"state": "ACCEPTED", "evidence_id": attempt["evidence_id"]}
                if run["state"] in {"SUCCEEDED", "FAILED", "CANCELLED"}:
                    return {"state": "TERMINAL_UNCHANGED", "evidence_id": None}
                if attempt["state"] != "DISPATCHED":
                    return {"state": attempt["state"], "evidence_id": attempt["evidence_id"]}
                error_object = self.objects.put({"schema_version": "tool_error@1",
                                                 "code": code, "message": message})
                c.execute(update(attempts).where(attempts.c.id == attempt_id).values(
                    state=state, outcome_class=outcome_class, error_code=code,
                    error_object=error_object, finished_at=now()))
                c.execute(update(steps).where(steps.c.id == step_id).values(state=state))
                unresolved = c.execute(select(attempts.c.id).where(attempts.c.run_id == run_id,
                    attempts.c.state.in_(["UNKNOWN", "DISPATCHED"])).limit(1)).first()
                c.execute(update(runs).where(runs.c.id == run_id).values(
                    state="WAITING_RECONCILIATION" if unresolved else "RUNNING"))
                payload = {"step_id": step_id, "attempt_id": attempt_id, "code": code,
                           "error_object": error_object, "retry": retry}
                if result_object is not None:
                    payload["result_object"] = result_object
                    payload["accepted_as_evidence"] = False
                self.store.event(c, run_id, event_name, payload)
                if state == "UNKNOWN":
                    self._checkpoint("unknown_mark_before_commit", run_id=run_id,
                        step_id=step_id, attempt_id=attempt_id, object_hash=error_object)
                elif state == "FAILED":
                    self._checkpoint("failed_mark_before_commit", run_id=run_id,
                        step_id=step_id, attempt_id=attempt_id, object_hash=error_object)
            return {"state": state, "evidence_id": None}
        except Exception:
            raise WorkbenchError("outcome_unknown", "Outcome could not be durably classified; explicit reconciliation is required", 409) from None

    def _check_retry_policy(self, c, run_id, tool_ref, args, spec):
        if spec.retry != "never":
            return
        failed_steps = c.execute(select(steps.c.decision_hash).where(
            steps.c.run_id == run_id,
            steps.c.state.in_(["FAILED", "REJECTED_AFTER_EXECUTION", "RESOLVED_FAILED"]))).scalars()
        for object_hash in failed_steps:
            prior = self.objects.get(object_hash)
            if prior["tool_ref"] == tool_ref and canonical(prior["args"]) == canonical(args):
                raise WorkbenchError("retry_forbidden", "This tool does not allow a new action for the same failed input", 409)

    def _skill_delivery(self, manifest, attachment=None):
        skill = self.objects.get(manifest["skill_object"])
        if digest(skill) != manifest["bindings"]["skill"]:
            raise WorkbenchError("version_mismatch", "Saved Skill does not match the run", 409)
        result = {"skill_ref": skill["ref"], "skill_digest": manifest["bindings"]["skill"],
                  "instructions": skill["instructions"], "attachments": [
                      {"filename": name, "sha256": hashlib.sha256(body.encode()).hexdigest()}
                      for name, body in sorted(skill.get("attachments", {}).items())]}
        if attachment is not None:
            if attachment not in skill.get("attachments", {}):
                raise WorkbenchError("attachment_not_found", "Unknown bound Skill attachment", 404)
            result["attachment"] = {"filename": attachment, "content": skill["attachments"][attachment]}
        return result

    def get_run_skill(self, run_id, attachment=None, *, lease=None):
        with self.store.engine.begin() as c:
            run = self._run(c, run_id, lock=not self.readonly)
            if self.readonly and run["state"] not in {"SUCCEEDED", "FAILED", "CANCELLED"}:
                raise WorkbenchError("skill_delivery_requires_writable", "Active Skill delivery needs writable audit metadata", 403)
            if not self.readonly and run["state"] not in {"SUCCEEDED", "FAILED", "CANCELLED"}:
                self._assert_submit(c, run, lease)
            result = self._skill_delivery(run["manifest"], attachment)
            if not self.readonly and run["state"] not in {"SUCCEEDED", "FAILED", "CANCELLED"}:
                self.store.event(c, run_id, "SKILL_DELIVERED", {"channel": "get_run_skill",
                    "attachment": attachment, "skill_digest": result["skill_digest"]})
        return result

    def create(self, request: RunRequest, idempotency_key: str, *, driver="external_agent", execution_config=None):
        self._writable()
        self._authorize(request.project_id, request.source_ref)
        if request.required_fact_columns or "required_fact_columns" in request.model_fields_set:
            raise WorkbenchError("legacy_result_contract", "New runs use result_spec; required_fact_columns is replay-only", 422)
        if request.result_spec is None or not request.result_spec.requirements:
            raise WorkbenchError("result_contract_required", "New runs require at least one necessary fact", 422)
        if driver not in {"external_agent", "policy_fixture", "internal_runner"}:
            raise WorkbenchError("invalid_driver", "Unknown execution origin")
        if not 1 <= len(idempotency_key) <= 128:
            raise WorkbenchError("invalid_key", "Idempotency key requires 1–128 characters")
        if driver == "internal_runner":
            from .execution import ExecutionConfig
            execution_config = ExecutionConfig.model_validate(execution_config).model_dump(mode="json")
        elif execution_config is not None:
            raise WorkbenchError("invalid_driver", "Execution configuration requires internal runner")
        request_hash = digest({"request": request.model_dump(mode="json"), "driver": driver, **({"execution_config": execution_config} if execution_config else {})})
        with self.store.engine.begin() as c:
            c.execute(text("SELECT pg_advisory_xact_lock(72408193)"))
            previous = c.execute(self._runs_select().where(runs.c.project_id == request.project_id,
                runs.c.actor == self.actor, runs.c.idempotency_key == idempotency_key)).mappings().first()
            if previous:
                if previous["request_digest"] != request_hash:
                    raise WorkbenchError("idempotency_conflict", "Same key was used for a different request", 409)
                self._check_cross_run(c, previous)
                if previous["state"] not in {"SUCCEEDED", "FAILED", "CANCELLED"}:
                    self.store.event(c, previous["id"], "SKILL_DELIVERED", {"channel": "create_replay",
                        "attachment": None, "skill_digest": previous["manifest"]["bindings"]["skill"]})
                return {"run_id": previous["id"], "state": previous["state"], "created": False,
                        "skill": self._skill_delivery(previous["manifest"])}
            adapter, skill = self.registry.resolve(request)
            skill_hash = self.objects.put(skill.model_dump(mode="json"))
            catalog_hash = self.objects.put(adapter.catalog())
            tools_hash = self.objects.put({k: v.model_dump(mode="json") for k, v in adapter.tools.items()})
            self.store.release(c, request.project_id + ":skill", skill.ref, skill_hash)
            self.store.release(c, request.project_id + ":source", adapter.ref, catalog_hash)
            for name, tool in adapter.tools.items():
                self.store.release(c, request.project_id + ":tool", name, digest(tool.model_dump(mode="json")))
            run_id = new_id("run")
            if request.daily_protocol:
                from .daily import admit
                admit(request,adapter)
            cutoff = getattr(adapter, "knowledge_cutoff", lambda p: None)(request.parameters)
            self._cutoff(cutoff)
            environment = capture_environment(adapter, self.entrypoints)
            manifest = {"schema_version": "run_manifest@2", "request": request.model_dump(mode="json"),
                "bindings": self._bindings(adapter, skill, environment), "skill_object": skill_hash,
                "environment_object": self.objects.put(environment), "knowledge_cutoff": cutoff,
                "catalog_object": catalog_hash, "tools_object": tools_hash,
                "store_class": self.objects.store_class,
                "privacy": adapter.privacy, "created_at": now(),
                "data_version": {"mode": "live", "observation_scope": new_id("live"),
                    "queryable_snapshot": False, "point_in_time_reconstruction": False,
                    "limitation": "Each query reads current data independently. Saved results support audit only."},
                "execution": {"driver": driver, "model": None, "provider_request_id": None,
                    "usage": None, "note": "External agent supplies visible tool decisions; provider telemetry is unavailable."},
                "validator": "cell_validator@1", "grader": None,
                "access_profile": "local_single_principal@1", "budget": {
                    "tool_calls": request.tool_budget, "report_attempts": request.report_budget}}
            if request.consistency == "frozen":
                manifest["data_version"] = adapter.data_version()
            if execution_config:
                manifest["execution"] = {"driver": driver, "config": execution_config,
                    "model": execution_config["model"], "provider": execution_config["provider"],
                    "provider_request_id": None, "usage": None, "fixture": execution_config["provider"] == "fixture"}
            self._checkpoint("create_after_cas_before_insert", run_id=run_id, object_hash=skill_hash)
            c.execute(insert(runs).values(id=run_id, project_id=request.project_id, actor=self.actor,
                idempotency_key=idempotency_key, request_digest=request_hash, manifest=manifest,
                state="ADMITTED", seq=0, tool_calls=0, created_at=now(), updated_at=now()))
            if request.daily_protocol:
                from .storage import daily_sessions
                c.execute(insert(daily_sessions).values(run_id=run_id,config=request.daily_protocol.model_dump(mode="json"),position=0))
            if execution_config:
                c.execute(insert(run_execution).values(run_id=run_id, config=execution_config))
            self.store.event(c, run_id, "RUN_ADMITTED", {"manifest_digest": digest(manifest), "consistency": request.consistency, "driver": driver})
            delivery = self._skill_delivery(manifest)
            self.store.event(c, run_id, "SKILL_DELIVERED", {"channel": "create", "attachment": None,
                "skill_digest": delivery["skill_digest"]})
        return {"run_id": run_id, "state": "ADMITTED", "created": True, "skill": delivery}

    def call(self, run_id: str, tool_ref: str, args: dict, action_key: str, explanation: str, *, lease=None):
        self._writable()
        decision = {"tool_ref": tool_ref, "args": args, "explanation": explanation,
                    "origin": "visible_agent_action", "schema_version": "tool_decision@1"}
        error = None
        with self.store.engine.begin() as c:
            run = self._run(c, run_id, lock=True)
            self._assert_submit(c, run, lease)
            existing = c.execute(select(steps).where(steps.c.run_id == run_id,
                                                    steps.c.action_key == action_key)).mappings().first()
            if run["state"] in {"SUCCEEDED", "FAILED", "CANCELLED"}:
                if existing and existing["state"] == "ACCEPTED":
                    try:
                        identical = existing["input_digest"] == digest(decision)
                    except (ValueError, TypeError):
                        identical = False
                    if identical:
                        return self.read_evidence(run_id, existing["evidence_id"])
                raise WorkbenchError("invalid_state", "Terminal runs do not accept new tool actions", 409)
            input_hash = digest(decision)
            if existing:
                if existing["input_digest"] != input_hash:
                    raise WorkbenchError("idempotency_conflict", "Action key has a different tool decision", 409)
                if existing["state"] == "ACCEPTED":
                    return self.read_evidence(run_id, existing["evidence_id"])
                if existing["state"] in {"DISPATCHED", "UNKNOWN"}:
                    raise WorkbenchError("outcome_unknown", "Action already dispatched; automatic retry is unavailable", 409)
                raise WorkbenchError("action_already_used", "This action key already has a durable outcome; use an allowed new action", 409)
            try:
                if run["state"] not in {"ADMITTED", "RUNNING"}:
                    raise WorkbenchError("invalid_state", "Run cannot dispatch tools in its current state", 409)
                if c.execute(select(steps.c.id).where(steps.c.run_id == run_id, steps.c.state == "DISPATCHED")).first():
                    raise WorkbenchError("busy", "A prior action is still in flight; reconcile before continuing", 409)
                if c.execute(select(attempts.c.id).where(attempts.c.run_id == run_id,
                                                         attempts.c.state == "UNKNOWN")).first():
                    raise WorkbenchError("unresolved_attempt", "An UNKNOWN attempt requires an operator resolution before continuing", 409)
                if not 1 <= len(action_key) <= 128 or len(explanation) > 4000:
                    raise WorkbenchError("invalid_action", "Invalid action identity or explanation")
                adapter, skill = self._current(run)
                if tool_ref not in skill.tools or tool_ref not in adapter.tools:
                    raise WorkbenchError("unknown_tool", "Tool is not in the pinned Skill's capability set", 422)
                spec = adapter.tools[tool_ref]
                try:
                    jsonschema.validate(args, spec.input_schema)
                    adapter.validate_call(tool_ref, args, self.effective_parameters(run))
                except (ValueError, jsonschema.ValidationError):
                    raise WorkbenchError("invalid_parameters", "Tool arguments violate the registered scope/schema", 422) from None
                self._check_retry_policy(c, run_id, tool_ref, args, spec)
                if run["tool_calls"] >= run["manifest"]["budget"]["tool_calls"]:
                    raise WorkbenchError("budget_exceeded", "Tool call budget exhausted", 409)
            except WorkbenchError as exc:
                error = exc
                self.store.event(c, run_id, "TOOL_REJECTED", {"code": exc.code, "decision_digest": input_hash})
            if error is None:
                step_id, attempt_id = new_id("step"), new_id("attempt")
                decision_hash = self.objects.put(decision)
                self._checkpoint("call_after_decision_cas", run_id=run_id,
                    step_id=step_id, attempt_id=attempt_id, object_hash=decision_hash)
                c.execute(insert(steps).values(id=step_id, run_id=run_id, action_key=action_key,
                    input_digest=input_hash, attempt_id=attempt_id, state="DISPATCHED",
                    decision_hash=decision_hash, created_at=now()))
                c.execute(insert(attempts).values(id=attempt_id, step_id=step_id,
                    run_id=run_id, seq=1, state="DISPATCHED", started_at=now()))
                c.execute(update(runs).where(runs.c.id == run_id).values(state="RUNNING", tool_calls=runs.c.tool_calls + 1))
                self.store.event(c, run_id, "TOOL_DISPATCHED", {"step_id": step_id, "attempt_id": attempt_id,
                                                           "decision_object": decision_hash})
        if error:
            raise error
        # Dispatch intent is committed before calling trusted adapter code. No implicit retry.
        self._checkpoint("after_intent_commit", run_id=run_id, step_id=step_id, attempt_id=attempt_id)
        try:
            result = adapter.execute(tool_ref, args, self.effective_parameters(run))
            self._checkpoint("after_adapter_return", run_id=run_id, step_id=step_id, attempt_id=attempt_id)
            if len(canonical(result.model_dump(mode="json"))) > spec.max_result_bytes:
                raise WorkbenchError("result_too_large", "Tool result exceeds its declared size limit", 422)
            evidence_id = new_id("evidence")
            envelope = {**result.model_dump(mode="json"), "schema_version": "evidence@1", "status": "ok",
                "evidence_id": evidence_id, "run_id": run_id, "project_id": run["project_id"],
                "step_id": step_id, "attempt_id": attempt_id, "tool_ref": tool_ref,
                "data_version": run["manifest"]["data_version"], "bindings": run["manifest"]["bindings"],
                "observed_at": now(), "query_descriptor": args, "error": None}
            object_hash = self.objects.put(envelope)
        except Exception as exc:
            code, message = self._safe_error(exc)
            state = "FAILED" if spec.side_effects == "none" else "UNKNOWN"
            outcome = self._record_tool_outcome(run_id, step_id, attempt_id, state,
                code, message, retry=spec.retry if state == "FAILED" else "never", lease=lease)
            if outcome["state"] == "ACCEPTED":
                return self.read_evidence(run_id, outcome["evidence_id"])
            if state == "UNKNOWN":
                raise WorkbenchError("outcome_unknown", "External effects are uncertain; explicit reconciliation is required", 409) from None
            raise WorkbenchError(code, message, 422) from None
        self._checkpoint("after_evidence_cas", run_id=run_id, step_id=step_id,
                         attempt_id=attempt_id, object_hash=object_hash)
        try:
            with self.store.engine.begin() as c:
                current = self._run(c, run_id, lock=True)
                self._assert_submit(c, current, lease)
                self._current(current)
                attempt = c.execute(select(attempts).where(attempts.c.id == attempt_id,
                    attempts.c.run_id == run_id)).mappings().one()
                if current["state"] != "RUNNING" or attempt["state"] != "DISPATCHED":
                    raise WorkbenchError("invalid_state", "Run no longer accepts tool outputs", 409)
                c.execute(insert(evidence).values(id=evidence_id, run_id=run_id, step_id=step_id,
                                                 object_hash=object_hash, created_at=now()))
                c.execute(update(attempts).where(attempts.c.id == attempt_id).values(
                    state="ACCEPTED", outcome_class="accepted", evidence_id=evidence_id, finished_at=now()))
                c.execute(update(steps).where(steps.c.id == step_id).values(state="ACCEPTED", evidence_id=evidence_id))
                self.store.event(c, run_id, "EVIDENCE_ACCEPTED", {"evidence_id": evidence_id,
                    "step_id": step_id, "attempt_id": attempt_id, "object_hash": object_hash})
                self._checkpoint("accept_before_commit", run_id=run_id, step_id=step_id,
                    attempt_id=attempt_id, evidence_id=evidence_id, object_hash=object_hash)
        except Exception as exc:
            code, message = self._safe_error(exc, acceptance=True)
            rejected_state = "REJECTED_AFTER_EXECUTION" if spec.side_effects == "none" else "UNKNOWN"
            outcome = self._record_tool_outcome(run_id, step_id, attempt_id,
                rejected_state, code, message, result_object=object_hash,
                retry=spec.retry if rejected_state == "REJECTED_AFTER_EXECUTION" else "never", lease=lease)
            if outcome["state"] == "ACCEPTED":
                return self.read_evidence(run_id, outcome["evidence_id"])
            if rejected_state == "UNKNOWN":
                raise WorkbenchError("outcome_unknown", "External effects are uncertain; explicit reconciliation is required", 409) from None
            raise WorkbenchError(code, message, 409) from None
        self._checkpoint("after_evidence_accept", run_id=run_id, step_id=step_id,
                         attempt_id=attempt_id, evidence_id=evidence_id, object_hash=object_hash)
        return envelope

    def effective_parameters(self, run):
        from .daily import effective_parameters
        return effective_parameters(self,run)

    def submit_decision(self, run_id, decision, *, lease=None):
        from .daily import submit
        return submit(self,run_id,decision,lease=lease)

    def append_decision_review(self, run_id, day, text, *, lease=None):
        from .daily import review
        return review(self,run_id,day,text,lease=lease)

    def read_evidence(self, run_id, evidence_id):
        with self.store.engine.connect() as c:
            run = self._run(c, run_id)
            row = c.execute(select(evidence).where(evidence.c.id == evidence_id,
                                                  evidence.c.run_id == run_id)).mappings().first()
            if not row:
                raise WorkbenchError("invalid_evidence", "Evidence is not accepted for this run", 404)
            result = self.objects.get(row["object_hash"])
            if result["run_id"] != run_id or result["bindings"] != run["manifest"]["bindings"] or result["data_version"] != run["manifest"]["data_version"]:
                raise WorkbenchError("version_mismatch", "Evidence does not match the run manifest", 409)
            return result

    def reconcile(self, run_id, *, older_than_seconds):
        """Explicitly classify stale dispatches; never rerun a tool or source query."""
        if (isinstance(older_than_seconds, bool)
                or not isinstance(older_than_seconds, (int, float))
                or (isinstance(older_than_seconds, float) and not math.isfinite(older_than_seconds))
                or older_than_seconds < 0):
            raise WorkbenchError("invalid_reconcile_threshold", "Reconciliation threshold must be a finite nonnegative number", 422)
        self._writable()
        reconciled = []
        observed_at = now()
        observed_time = datetime.fromisoformat(observed_at)
        with self.store.engine.begin() as c:
            run = self._run(c, run_id, lock=True, apply_cutoff=False)
            if run["state"] in {"SUCCEEDED", "FAILED", "CANCELLED"}:
                raise WorkbenchError("invalid_state", "Terminal runs cannot be reconciled", 409)
            dispatched = c.execute(select(attempts).where(attempts.c.run_id == run_id,
                attempts.c.state == "DISPATCHED").order_by(attempts.c.started_at, attempts.c.id)).mappings().all()
            for attempt in dispatched:
                try:
                    started = datetime.fromisoformat(attempt["started_at"])
                    if started.tzinfo is None:
                        raise ValueError("Timezone missing")
                    age = (observed_time - started).total_seconds()
                except (ValueError, TypeError, OverflowError):
                    raise WorkbenchError("invalid_attempt_timestamp", "Attempt time cannot be safely compared; reconciliation was refused", 409) from None
                if age < older_than_seconds:
                    continue
                code = "dispatch_unconfirmed"
                error_object = self.objects.put({"schema_version": "tool_error@1", "code": code,
                    "message": "A dispatched attempt exceeded the reconciliation threshold without durable acceptance."})
                c.execute(update(attempts).where(attempts.c.id == attempt["id"]).values(
                    state="UNKNOWN", outcome_class="uncertain", error_code=code,
                    error_object=error_object, finished_at=observed_at))
                c.execute(update(steps).where(steps.c.id == attempt["step_id"]).values(state="UNKNOWN"))
                c.execute(update(runs).where(runs.c.id == run_id).values(state="WAITING_RECONCILIATION"))
                self.store.event(c, run_id, "TOOL_OUTCOME_UNCERTAIN", {
                    "step_id": attempt["step_id"], "attempt_id": attempt["id"], "code": code,
                    "error_object": error_object, "retry": "never", "reconciliation": True,
                    "older_than_seconds": older_than_seconds, "reconciled_by": self.actor})
                self._checkpoint("unknown_mark_before_commit", run_id=run_id,
                    step_id=attempt["step_id"], attempt_id=attempt["id"], object_hash=error_object)
                reconciled.append(attempt["id"])
        return {"run_id": run_id, "state": "WAITING_RECONCILIATION" if reconciled else run["state"],
                "reconciled": reconciled, "count": len(reconciled)}

    def resolve(self, run_id, attempt_id, *, actor, reason,
                disposition="confirm_no_side_effects_failed"):
        """Apply a recorded operator decision to a pinned, explicitly read-only tool."""
        self._writable()
        if (not isinstance(actor, str) or not actor.strip() or len(actor) > 200
                or not isinstance(reason, str) or not reason.strip() or len(reason) > 4000
                or disposition != "confirm_no_side_effects_failed"):
            raise WorkbenchError("invalid_resolution", "Resolution requires a bounded operator name, reason, and supported disposition", 422)
        with self.store.engine.begin() as c:
            run = self._run(c, run_id, lock=True, apply_cutoff=False)
            if run["state"] in {"SUCCEEDED", "FAILED", "CANCELLED"}:
                raise WorkbenchError("invalid_state", "This run cannot accept an operator resolution", 409)
            attempt = c.execute(select(attempts).where(attempts.c.id == attempt_id,
                                                       attempts.c.run_id == run_id)).mappings().first()
            if attempt is None:
                raise WorkbenchError("invalid_attempt", "Attempt is not part of this run", 404)
            if run["state"] not in {"RUNNING", "WAITING_RECONCILIATION"} or attempt["state"] != "UNKNOWN":
                raise WorkbenchError("invalid_state", "Only an UNKNOWN attempt may be resolved", 409)
            step = c.execute(select(steps).where(steps.c.id == attempt["step_id"],
                                                 steps.c.run_id == run_id)).mappings().one()
            decision = self.objects.get(step["decision_hash"])
            pinned_tools = self.objects.get(run["manifest"]["tools_object"])
            if digest(pinned_tools) != run["manifest"]["bindings"]["tools"]:
                raise WorkbenchError("version_mismatch", "Pinned tool definitions do not match the run manifest", 409)
            pinned_tool = pinned_tools.get(decision.get("tool_ref"))
            effects = pinned_tool.get("side_effects") if isinstance(pinned_tool, dict) else None
            if effects not in {"none", "external"}:
                raise WorkbenchError("side_effects_unverified", "The saved tool release does not explicitly establish its side effects", 409)
            if effects != "none":
                raise WorkbenchError("side_effects_external", "An external-effects tool cannot use the read-only resolution", 409)
            resolution_id = new_id("resolution")
            at = now()
            c.execute(insert(resolutions).values(id=resolution_id, attempt_id=attempt_id,
                step_id=attempt["step_id"], run_id=run_id, actor=actor, reason=reason,
                disposition=disposition, created_at=at))
            c.execute(update(attempts).where(attempts.c.id == attempt_id).values(
                state="RESOLVED_FAILED", outcome_class="resolved_failure", finished_at=at))
            c.execute(update(steps).where(steps.c.id == attempt["step_id"]).values(state="RESOLVED_FAILED"))
            remaining = c.execute(select(attempts.c.id).where(attempts.c.run_id == run_id,
                attempts.c.state.in_(["UNKNOWN", "DISPATCHED"])).limit(1)).first()
            next_state = "WAITING_RECONCILIATION" if remaining else "RUNNING"
            c.execute(update(runs).where(runs.c.id == run_id).values(state=next_state))
            self.store.event(c, run_id, "ATTEMPT_RESOLVED", {"resolution_id": resolution_id,
                "attempt_id": attempt_id, "step_id": attempt["step_id"], "actor": actor,
                "reason": reason, "disposition": disposition, "authenticated_actor": self.actor})
        return {"run_id": run_id, "state": next_state, "resolution_id": resolution_id,
                "attempt_id": attempt_id, "attempt_state": "RESOLVED_FAILED"}

    def finalize(self, run_id: str, report: Report, *, lease=None):
        self._writable()
        with self.store.engine.begin() as c:
            run = self._run(c, run_id, lock=True)
            self._assert_submit(c, run, lease)
            if run["manifest"]["request"].get("daily_protocol"):
                from .daily import require_complete
                require_complete(c,run)
            if run["state"] in {"SUCCEEDED", "FAILED", "CANCELLED"}:
                raise WorkbenchError("invalid_state", "Terminal runs do not accept report submissions", 409)
            if run["state"] not in {"ADMITTED", "RUNNING"}:
                raise WorkbenchError("invalid_state", "Only an admitted or running task can submit a report", 409)
            self._current(run)
            if c.execute(select(steps.c.id).where(steps.c.run_id == run_id,
                                                 steps.c.state.in_(["DISPATCHED", "UNKNOWN"]))).first():
                raise WorkbenchError("unresolved_attempt", "Resolve in-flight or uncertain attempts first", 409)
            report_budget = run["manifest"].get("budget", {}).get("report_attempts", 3)
            if run["report_attempts"] >= report_budget:
                raise WorkbenchError("budget_exceeded", "Report submission budget exhausted", 409)
            c.execute(update(runs).where(runs.c.id == run_id).values(report_attempts=runs.c.report_attempts + 1))
            report_dict = report.model_dump(mode="json")
            empty_admitted = run["state"] == "ADMITTED" and run["tool_calls"] == 0
            finished_attempts = list(c.execute(select(attempts.c.id, attempts.c.state).where(
                attempts.c.run_id == run_id)).mappings())
            no_evidence = not c.execute(select(evidence.c.id).where(evidence.c.run_id == run_id).limit(1)).first()
            all_failed = bool(finished_attempts) and no_evidence and all(
                a["state"] in {"FAILED", "REJECTED_AFTER_EXECUTION", "RESOLVED_FAILED"} for a in finished_attempts)
            validation = validate_report(report, lambda e: self.read_evidence(run_id, e),
                run["manifest"]["request"].get("required_fact_columns", []),
                result_spec=run["manifest"]["request"].get("result_spec"),
                catalog=self.objects.get(run["manifest"]["catalog_object"]),
                allow_empty_insufficient=empty_admitted or all_failed)
            if all_failed and report.result_status == "insufficient_evidence" and not report.facts:
                validation["required_fact_coverage"]["exception"] = "all_attempts_failed_insufficient_evidence"
                validation["failed_attempt_ids"] = sorted(a["id"] for a in finished_attempts)
            if empty_admitted and report.result_status != "insufficient_evidence":
                validation["status"] = "blocked"
                validation["errors"].append({"code": "zero_tool_requires_insufficient_evidence"})
            artifact = {"schema_version": "validated_report@1", "run_id": run_id,
                        "manifest_digest": digest(run["manifest"]), "report": report_dict,
                        "validation": validation, "created_at": now()}
            report_hash = self.objects.put(artifact)
            if validation["status"] == "blocked":
                self.store.event(c, run_id, "REPORT_BLOCKED", {"candidate_object": report_hash,
                    "errors": validation["errors"]})
                if run["report_attempts"] + 1 == report_budget:
                    c.execute(update(runs).where(runs.c.id == run_id).values(state="FAILED"))
                    self.store.event(c, run_id, "RUN_FAILED", {"reason": "report_budget_exhausted"})
            else:
                c.execute(update(runs).where(runs.c.id == run_id).values(state="SUCCEEDED", report_hash=report_hash))
                self.store.event(c, run_id, "REPORT_PUBLISHED", {"report_object": report_hash,
                    "result_status": report.result_status, "validation_status": validation["status"]})
                self._checkpoint("during_report_publish", run_id=run_id, object_hash=report_hash)
        return artifact

    def reopen(self, run_id: str):
        # No adapter calls, model calls or implicit recovery. All objects are checksum verified.
        with self.store.engine.connect() as c:
            run = self._run(c, run_id)
            event_rows = [dict(r) for r in c.execute(select(events).where(events.c.run_id == run_id).order_by(events.c.seq)).mappings()]
            step_rows = [dict(r) for r in c.execute(select(steps).where(steps.c.run_id == run_id).order_by(steps.c.created_at)).mappings()]
            attempt_rows = [dict(r) for r in c.execute(select(attempts).where(
                attempts.c.run_id == run_id).order_by(attempts.c.started_at, attempts.c.seq)).mappings()]
            resolution_rows = [dict(r) for r in c.execute(select(resolutions).where(
                resolutions.c.run_id == run_id).order_by(resolutions.c.created_at)).mappings()]
            model_rows = []
            if run["manifest"].get("execution", {}).get("driver") == "internal_runner":
                model_rows = [dict(row) for row in c.execute(select(model_invocations).where(
                    model_invocations.c.run_id == run_id).order_by(model_invocations.c.at, model_invocations.c.invocation_id, model_invocations.c.seq)).mappings()]
        daily_record = None
        if run["manifest"]["request"].get("daily_protocol"):
            from .daily import record
            daily_record = record(self,run_id)
        for invocation in model_rows:
            payload = invocation["payload"]
            invocation["objects"] = {key: self.objects.get(payload[key]) for key in
                ("input_object", "response_object", "outcome_object") if payload.get(key)}
        for step in step_rows:
            step["decision"] = self.objects.get(step["decision_hash"])
            step["evidence"] = self.read_evidence(run_id, step["evidence_id"]) if step["evidence_id"] else None
        for attempt in attempt_rows:
            attempt["error"] = self.objects.get(attempt["error_object"]) if attempt["error_object"] else None
        return {"run": run, "events": event_rows, "steps": step_rows, **({"daily":daily_record} if daily_record else {}),
            "attempts": attempt_rows, "resolutions": resolution_rows, **({"model_invocations": model_rows} if model_rows else {}),
            "skill": (self.objects.get(run["manifest"]["skill_object"])
                      if run["state"] in {"SUCCEEDED", "FAILED", "CANCELLED"} else
                      {"ref": run["manifest"]["request"]["skill_ref"],
                       "digest": run["manifest"]["bindings"]["skill"]}),
            "catalog": self.objects.get(run["manifest"]["catalog_object"]),
            "tools": self.objects.get(run["manifest"]["tools_object"]),
            "report": self.objects.get(run["report_hash"]) if run["report_hash"] else None,
            "operation": "replay_saved_records", "source_queries_executed": 0,
            "frozen_rerun_available": run["manifest"]["data_version"].get("queryable_snapshot", False)}

    def replay(self, run_id):
        return self.reopen(run_id)

    def rerun_frozen(self, run_id, idempotency_key):
        prior = self.reopen(run_id)["run"]
        request = RunRequest(**prior["manifest"]["request"])
        if request.consistency != "frozen":
            raise WorkbenchError("unsupported", "Original run has no bound queryable snapshot", 422)
        if prior["idempotency_key"] == idempotency_key:
            raise WorkbenchError("new_run_key_required", "Rerun needs a new idempotency key", 409)
        # Admission verifies the registered snapshot, not saved query results.
        return self.create(request, idempotency_key)

    def run_latest(self, run_id, source_ref, idempotency_key):
        prior = self.reopen(run_id)["run"]
        data = {**prior["manifest"]["request"], "source_ref":source_ref,"consistency":"live"}
        return self.create(RunRequest(**data), idempotency_key)

    def history(self, project_id):
        self._authorize(project_id)
        with self.store.engine.connect() as c:
            rows = c.execute(self._runs_select().where(runs.c.project_id == project_id, runs.c.actor == self.actor)
                .order_by(runs.c.created_at.desc())).mappings()
            result = []
            for row in rows:
                try:
                    self._authorize(project_id, row["manifest"]["request"]["source_ref"], read=True)
                except WorkbenchError:
                    continue
                if self._cross_run_blocker(c, row) is None:
                    result.append({k: row[k] for k in ("id", "state", "created_at", "updated_at")})
                    if len(result) == 200:
                        break
            return result

    def unsupported(self, operation: str):
        raise WorkbenchError("unsupported", f"{operation} is not implemented in this slice", 422)
