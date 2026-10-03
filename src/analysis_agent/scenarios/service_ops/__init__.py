"""Public synthetic service investigation; no network or database source access."""
from copy import deepcopy
from datetime import datetime, timezone
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path

from ...contracts import SkillRelease, TableResult, ToolSpec, WorkbenchError, digest
from ._fixture import DATA, SKILL_TEXT

EMPTY = {"type": "object", "properties": {}, "additionalProperties": False}
UNITS = {"window": "label", "service": "identifier", "requests": "requests",
         "errored_requests": "requests", "error_rate": "ratio", "mean_duration": "ms"}


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
    return {"service": "edge", "baseline_start": "2026-01-01T00:00:00Z",
            "baseline_end": "2026-01-01T01:00:00Z", "current_start": "2026-01-02T00:00:00Z",
            "current_end": "2026-01-02T01:00:00Z", "as_of": "2026-01-02T01:00:00Z"}


def unique(rows, key):
    selected = {}
    for row in rows:
        if row[key] in selected and selected[row[key]] != row:
            raise WorkbenchError("data_conflict", "Conflicting duplicate entity identity", 422)
        selected[row[key]] = row
    return list(selected.values())


class ServiceOpsAdapter:
    data_contract = "service_ops@1"
    privacy = "public"

    def __init__(self, data=None):
        self._data = deepcopy(DATA if data is None else data)
        self.ref = "src_" + digest(self._data)[:24] + "@1"
        self.tools = {name: ToolSpec(ref=name, capability="service_ops." + name.split("@")[0],
                                   description=description, input_schema=EMPTY,
                                   side_effects="none", retry="new_action_allowed")
                      for name, description in {
                          "service_overview@1": "Compare the fixed, equal-duration current and baseline windows. Distinct requests prevent error-event fanout. Fewer than four samples bounds evidence. Correlation is not causation.",
                          "service_context@1": "Read release and alert records within the fixed windows and availability cutoff. Correlation is not causation; releases do not prove a cause."
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
                "grain": {"requests": "one request", "errors": "one error event; many per request",
                          "context": "one release or alert"},
                "entity_keys": {"requests": ["request_id"], "errors": ["error_id"], "context": ["event_id"]},
                "fields": {"requests": {"request_id": "string", "service": "string", "observed_at": "timestamp",
                            "available_at": "timestamp", "duration": "number", "duration_unit": "string"},
                           "errors": {"error_id": "string", "request_id": "string", "observed_at": "timestamp", "available_at": "timestamp"},
                           "context": {"event_id": "string", "service": "string", "kind": "string", "observed_at": "timestamp",
                                       "available_at": "timestamp", "detail": "string"}},
                "time_fields": ["observed_at", "available_at"], "intervals": "start <= observed_at < end; available_at < as_of; observed_at < as_of",
                "coverage": {"start": "2026-01-01T00:00:00Z", "end": "2026-01-03T00:00:00Z"},
                "baseline_policy": "Caller selects equal-duration non-overlapping earlier baseline; no automatic causal control.",
                "minimum_samples": 4, "units": UNITS,
                "metrics": {"requests": "Distinct request_id in cohort, before availability cutoff",
                            "errored_requests": "Distinct cohort request_id with any error before cutoff; never event count",
                            "error_rate": "errored_requests / requests; zero denominator is an error",
                            "mean_duration": "Sum one ms duration per distinct request / requests; mixed units are an error"},
                "reference_time": {"selector": "window", "windows": {
                    name: {"start": name + "_start", "end": name + "_end"} for name in ("current", "baseline")}},
                "derivation_policy": {"operations": {
                    op: {"input_units": units, "decimal_places": 8, "rounding": "ROUND_HALF_UP", "tolerance": "0.000000005"}
                    for op, units in {"subtract": ["requests", "ms"], "ratio": ["requests", "ms"],
                                      "pct_change": ["requests", "ms"], "pp_change": ["ratio"]}.items()}},
                "limitations": ["Public synthetic fixture, no model evaluation", "Correlation is not causation",
                                "Small samples support partial or insufficient_evidence only", "Live fixture is not a frozen backend"]}

    def validate_task(self, parameters):
        if not isinstance(parameters, dict) or set(parameters) != set(default_parameters()) or parameters["service"] != "edge":
            raise WorkbenchError("invalid_parameters", "Only the registered service and exact window fields are accepted", 422)
        p = {k: timestamp(v) for k, v in parameters.items() if k != "service"}
        low, high = timestamp("2026-01-01T00:00:00Z"), timestamp("2026-01-03T00:00:00Z")
        for name in ("current", "baseline"):
            if not low <= p[name + "_start"] < p[name + "_end"] <= p["as_of"] <= high:
                raise WorkbenchError("out_of_scope", "Window/cutoff outside declared coverage", 422)
        if (p["baseline_end"] > p["current_start"] or
                p["baseline_end"] - p["baseline_start"] != p["current_end"] - p["current_start"]):
            raise WorkbenchError("invalid_parameters", "Baseline must precede current and have equal duration", 422)

    def validate_call(self, tool_ref, args, parameters):
        self.validate_task(parameters)
        if tool_ref not in self.tools:
            raise WorkbenchError("unknown_tool", "Unregistered tool", 422)
        if not isinstance(args, dict) or args:
            raise WorkbenchError("invalid_parameters", "Tools cannot override admitted scope", 422)

    def execute(self, tool_ref, args, parameters):
        self.validate_call(tool_ref, args, parameters)
        p = {k: timestamp(v) for k, v in parameters.items() if k != "service"}
        rows, bounded = [], False
        def visible(row):
            return timestamp(row["available_at"]) < p["as_of"] and timestamp(row["observed_at"]) < p["as_of"]
        for window in ("current", "baseline"):
            def in_window(row):
                return p[window + "_start"] <= timestamp(row["observed_at"]) < p[window + "_end"]
            if tool_ref == "service_context@1":
                rows.extend({**r, "observed_at": iso(timestamp(r["observed_at"])), "window": window}
                            for r in unique(self._data["context"], "event_id")
                            if visible(r) and in_window(r) and r["service"] == parameters["service"])
                continue
            cohort = [r for r in unique(self._data["requests"], "request_id")
                      if visible(r) and in_window(r) and r["service"] == parameters["service"]]
            ids = {r["request_id"] for r in cohort}
            if not ids:
                raise WorkbenchError("zero_denominator", "Cannot compute a rate for an empty request cohort", 422)
            if any(r["duration_unit"] != "ms" for r in cohort):
                raise WorkbenchError("incompatible_units", "Durations must all be ms; no implicit conversion", 422)
            errored = {r["request_id"] for r in unique(self._data["errors"], "error_id") if r["request_id"] in ids and visible(r)}
            def divide(numerator):
                return format((Decimal(numerator) / len(ids)).quantize(Decimal("0.00000001"), rounding=ROUND_HALF_UP), "f")
            rows.append({"window": window, "service": parameters["service"], "requests": len(ids),
                         "errored_requests": len(errored), "error_rate": divide(len(errored)),
                         "mean_duration": divide(sum(Decimal(str(r["duration"])) for r in cohort))})
            bounded |= len(ids) < 4
        context = tool_ref == "service_context@1"
        return TableResult(rows=rows, units=UNITS if not context else {
            "window": "label", "service": "identifier", "event_id": "identifier", "kind": "label",
            "observed_at": "timestamp", "available_at": "timestamp", "detail": "text"},
            time_range={k: iso(v) for k, v in p.items()},
            entity_keys=["window", "service", "event_id"] if context else ["window", "service"],
            completeness="bounded" if bounded else "complete_for_query",
            warnings=["Small sample: partial or insufficient_evidence required"] if bounded else [],
            provenance={"source_ref": self.ref, "source_content_sha256": digest(self._data), "origin": "public synthetic fixture"})


def public_skill():
    text = SKILL_TEXT
    return SkillRelease(ref="skill_" + digest(text)[:24] + "@1", instructions=text,
                        data_contract="service_ops@1", tools=("service_overview@1", "service_context@1"))
