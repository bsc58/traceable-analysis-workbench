"""Public batch/check/dependency investigation with explicit evidence limits."""
from copy import deepcopy
from datetime import datetime, timezone
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path

from ...contracts import SkillRelease, TableResult, ToolSpec, WorkbenchError, digest
from ._fixture import DATA, SKILL_TEXT

EMPTY = {"type": "object", "properties": {}, "additionalProperties": False}
UNITS = {"dataset": "identifier", "batch_id": "identifier", "version": "label", "previous_version": "label",
         "version_changed": "boolean", "rows": "records", "checked_records": "records", "failed_records": "records",
         "failure_rate": "ratio", "freshness_seconds": "seconds", "fresh": "boolean"}


def timestamp(value):
    try:
        stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if "T" not in value or stamp.tzinfo is None:
            raise ValueError()
        return stamp.astimezone(timezone.utc)
    except (ValueError, TypeError, AttributeError):
        raise WorkbenchError("invalid_parameters", "An explicit timezone-qualified timestamp is required", 422) from None


def iso(value):
    return value.isoformat().replace("+00:00", "Z")


def default_parameters():
    return {"dataset": "orders", "as_of": "2026-01-02T01:00:00Z", "max_age_seconds": 3600}


def unique(rows, key):
    selected = {}
    for row in rows:
        if row[key] in selected and selected[row[key]] != row:
            raise WorkbenchError("data_conflict", "Conflicting duplicate entity identity", 422)
        selected[row[key]] = row
    return list(selected.values())


class DataQualityAdapter:
    data_contract = "data_quality@1"
    privacy = "public"

    def __init__(self, data=None):
        self._data = deepcopy(DATA if data is None else data)
        self.ref = "src_" + digest(self._data)[:24] + "@1"
        self.tools = {name: ToolSpec(ref=name, capability="data_quality." + name.split("@")[0],
                                   description=description, input_schema=EMPTY,
                                   side_effects="none", retry="new_action_allowed")
                      for name, description in {
                          "quality_overview@1": "Inspect latest available target batch, previous version, freshness and checks. Count distinct records before joining multiple rules; version-mismatched or incomplete checks bound evidence.",
                          "quality_dependencies@1": "Inspect declared direct upstream edges and latest available batches. Missing or version-incompatible upstream evidence is unknown; stop rather than infer healthy lineage. This is not transitive causal proof."
                      }.items()}

    def capabilities(self):
        return {"live": True, "frozen": False, "cancel": False, **{t.capability: True for t in self.tools.values()}}

    def knowledge_cutoff(self, parameters):
        self.validate_task(parameters)
        return iso(timestamp(parameters["as_of"]))

    def catalog(self):
        return {"source_ref": self.ref, "data_contract": self.data_contract, "privacy": "public",
                "source_content_sha256": digest(self._data),
                "implementation_sha256": digest({p.name: p.read_text() for p in sorted(Path(__file__).parent.glob("*.py"))}),
                "capabilities": self.capabilities(), "time_zone": "UTC", "source_timezone": "explicit RFC3339 offsets",
                "entity_keys": {"batches": ["batch_id"], "checks": ["check_id"], "dependencies": ["edge_id"]},
                "grain": {"batches": "one import batch", "checks": "one rule observation per batch and record",
                          "dependencies": "one declared direct upstream edge"},
                "fields": {"batches": {"batch_id": "string", "dataset": "string", "version": "string",
                            "imported_at": "timestamp", "available_at": "timestamp", "rows": "integer", "row_unit": "string"},
                           "checks": {"check_id": "string", "batch_id": "string", "record_id": "string", "rule": "string",
                                      "version": "string", "passed": "boolean", "checked_at": "timestamp", "available_at": "timestamp"},
                           "dependencies": {"edge_id": "string", "dataset": "string", "upstream": "string", "required_version": "string"}},
                "time_fields": ["imported_at", "checked_at", "available_at"],
                "intervals": "imported_at/checked_at < as_of AND available_at < as_of",
                "latest_policy": "Maximum imported_at before cutoff; distinct batch IDs tied in time are an error",
                "coverage": {"start": "2026-01-01T00:00:00Z", "end": "2026-01-03T00:00:00Z"},
                "units": UNITS,
                "metrics": {"rows": "Batch record count, counted once regardless of rule fanout",
                            "checked_records": "Distinct latest-batch record_id with matching-version rule results",
                            "failed_records": "Distinct checked record_id failing any matching-version rule",
                            "failure_rate": "failed_records / checked_records; zero denominator raises an error",
                            "freshness_seconds": "as_of minus latest imported_at (seconds); not market/event freshness",
                            "fresh": "freshness_seconds <= caller max_age_seconds",
                            "version_changed": "Latest version differs from previous visible batch; null if no previous batch",
                            "upstream_rows": "Latest compatible upstream batch record count; null if absent or version mismatch"},
                "reference_time": {"meaning": "point-in-time availability cutoff; evidence time_range start=end=as_of"},
                "derivation_policy": {"operations": {
                    op: {"input_units": units, "decimal_places": 8, "rounding": "ROUND_HALF_UP", "tolerance": "0.000000005"}
                    for op, units in {"subtract": ["records", "seconds"], "ratio": ["records", "seconds"],
                                      "pct_change": ["records", "seconds"], "pp_change": ["ratio"]}.items()}},
                "limitations": ["Synthetic import-time freshness, not source arrival guarantees",
                                "Direct dependencies only; missing upstream evidence must not be assumed healthy",
                                "Rule coverage is not a guarantee of semantic data correctness", "Live fixture only"]}

    def validate_task(self, parameters):
        if not isinstance(parameters, dict) or set(parameters) != set(default_parameters()) or parameters["dataset"] != "orders":
            raise WorkbenchError("invalid_parameters", "Only the registered dataset and exact task fields are accepted", 422)
        age = parameters["max_age_seconds"]
        if type(age) is not int or not 1 <= age <= 86400:
            raise WorkbenchError("invalid_parameters", "max_age_seconds must be an integer in [1,86400]", 422)
        if not timestamp("2026-01-01T00:00:00Z") < timestamp(parameters["as_of"]) <= timestamp("2026-01-03T00:00:00Z"):
            raise WorkbenchError("out_of_scope", "Cutoff outside declared coverage", 422)

    def validate_call(self, tool_ref, args, parameters):
        self.validate_task(parameters)
        if tool_ref not in self.tools:
            raise WorkbenchError("unknown_tool", "Unregistered tool", 422)
        if not isinstance(args, dict) or args:
            raise WorkbenchError("invalid_parameters", "Tools cannot override admitted scope", 422)

    def _batches(self, dataset, cutoff):
        rows = sorted((r for r in unique(self._data["batches"], "batch_id") if r["dataset"] == dataset
                       and timestamp(r["imported_at"]) < cutoff and timestamp(r["available_at"]) < cutoff),
                      key=lambda r: timestamp(r["imported_at"]), reverse=True)
        if len({timestamp(r["imported_at"]) for r in rows}) != len(rows):
            raise WorkbenchError("data_conflict", "Batch ordering is ambiguous at equal import timestamps", 422)
        if any(r["row_unit"] != "records" for r in rows):
            raise WorkbenchError("incompatible_units", "Batch counts must all use records", 422)
        return rows

    def execute(self, tool_ref, args, parameters):
        self.validate_call(tool_ref, args, parameters)
        cutoff = timestamp(parameters["as_of"])
        rows, incomplete = [], False
        dependencies = tool_ref == "quality_dependencies@1"
        if dependencies:
            for edge in unique(self._data["dependencies"], "edge_id"):
                if edge["dataset"] != parameters["dataset"]:
                    continue
                batches = self._batches(edge["upstream"], cutoff)
                latest = batches[0] if batches else None
                compatible = latest is not None and latest["version"] == edge["required_version"]
                incomplete |= not compatible
                rows.append({**edge, "upstream_rows": latest["rows"] if compatible else None,
                             "observed_version": latest["version"] if latest else None, "compatible": compatible,
                             "freshness_seconds": int((cutoff - timestamp(latest["imported_at"])).total_seconds()) if latest else None})
            incomplete |= not bool(rows)
        else:
            batches = self._batches(parameters["dataset"], cutoff)
            if not batches:
                raise WorkbenchError("insufficient_evidence", "No target batch available at cutoff", 422)
            latest = batches[0]
            checks = [r for r in unique(self._data["checks"], "check_id") if r["batch_id"] == latest["batch_id"]
                      and timestamp(r["checked_at"]) < cutoff and timestamp(r["available_at"]) < cutoff]
            matched = [r for r in checks if r["version"] == latest["version"]]
            checked = {r["record_id"] for r in matched}
            failed = {r["record_id"] for r in matched if not r["passed"]}
            if not checked:
                raise WorkbenchError("zero_denominator", "No version-compatible checked records; rate is undefined", 422)
            if len(checked) > latest["rows"]:
                raise WorkbenchError("data_conflict", "Checked distinct records exceed batch size", 422)
            incomplete = len(checked) < latest["rows"] or len(matched) != len(checks) or len(batches) < 2
            age = int((cutoff - timestamp(latest["imported_at"])).total_seconds())
            previous = batches[1]["version"] if len(batches) > 1 else None
            rows.append({"dataset": parameters["dataset"], "batch_id": latest["batch_id"], "version": latest["version"],
                         "previous_version": previous, "version_changed": latest["version"] != previous if previous else None,
                         "rows": latest["rows"], "checked_records": len(checked), "failed_records": len(failed),
                         "failure_rate": format((Decimal(len(failed)) / len(checked)).quantize(Decimal("0.00000001"), rounding=ROUND_HALF_UP), "f"),
                         "freshness_seconds": age, "fresh": age <= parameters["max_age_seconds"]})
        stamp = iso(cutoff)
        return TableResult(rows=rows, units=UNITS if not dependencies else {
            "edge_id": "identifier", "dataset": "identifier", "upstream": "identifier", "required_version": "label",
            "observed_version": "label", "compatible": "boolean", "upstream_rows": "records", "freshness_seconds": "seconds"},
            time_range={"start": stamp, "end": stamp}, entity_keys=["dataset", "upstream"] if dependencies else ["dataset"],
            completeness="unknown" if incomplete and dependencies else "bounded" if incomplete else "complete_for_query",
            warnings=["Missing/incompatible evidence: do not claim complete; stop if tools cannot resolve it"] if incomplete else [],
            provenance={"source_ref": self.ref, "source_content_sha256": digest(self._data), "origin": "public synthetic fixture"})


def public_skill():
    text = SKILL_TEXT
    return SkillRelease(ref="skill_" + digest(text)[:24] + "@1", instructions=text,
                        data_contract="data_quality@1", tools=("quality_overview@1", "quality_dependencies@1"))
