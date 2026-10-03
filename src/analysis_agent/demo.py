"""Independent synthetic commerce adapter; a live fixture, not a frozen backend.

The records and amounts below are invented for this public example. No external
database, proprietary schema, production record or private Skill is needed.
"""
from __future__ import annotations

import json
from pathlib import Path

from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Any

from .contracts import SkillRelease, TableResult, ToolSpec, WorkbenchError, digest


UTC = timezone.utc
WINDOW_START = datetime(2025, 1, 1, tzinfo=UTC)
WINDOW_END = datetime(2025, 1, 15, tzinfo=UTC)
OBSERVATION_END = datetime(2025, 1, 16, tzinfo=UTC)
PARAMETERS = {"current_start", "current_end", "baseline_start", "baseline_end", "as_of"}
EMPTY_ARGS = {"type": "object", "properties": {}, "additionalProperties": False}
CHANNEL_ARGS = {
    "type": "object", "properties": {"dimension": {"const": "channel"}},
    "required": ["dimension"], "additionalProperties": False,
}
UNITS = {
    "window": "label", "channel": "label", "orders": "orders",
    "refunded_orders": "orders", "refund_rate": "ratio",
    "order_amount": "USD", "refund_amount": "USD",
}


def default_parameters() -> dict[str, str]:
    return {
        "current_start": "2025-01-08T00:00:00Z",
        "current_end": "2025-01-15T00:00:00Z",
        "baseline_start": "2025-01-01T00:00:00Z",
        "baseline_end": "2025-01-08T00:00:00Z",
        "as_of": "2025-01-15T00:00:00Z",
    }


def _iso(value: datetime) -> str:
    return value.isoformat().replace("+00:00", "Z")


def _utc(value: Any) -> datetime:
    if not isinstance(value, str):
        raise WorkbenchError("invalid_parameters", "Timestamps must be explicit UTC strings", 422)
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise WorkbenchError("invalid_parameters", "Invalid RFC3339 timestamp", 422) from exc
    if parsed.tzinfo is None or parsed.utcoffset() != timedelta(0):
        raise WorkbenchError("invalid_parameters", "Timestamps must specify UTC", 422)
    # datetime accepts date-only strings; a UTC offset and time are required here.
    if "T" not in value:
        raise WorkbenchError("invalid_parameters", "A timestamp with time is required", 422)
    return parsed.astimezone(UTC)


def _money(value: Decimal) -> str:
    return str(value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def _rate(numerator: int, denominator: int) -> str | None:
    if not denominator:
        return None
    return str((Decimal(numerator) / Decimal(denominator)).quantize(
        Decimal("0.00000001"), rounding=ROUND_HALF_UP))


def _records(variant: str) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    if variant not in {"normal", "anomaly"}:
        raise ValueError("Unknown synthetic case; choose normal or anomaly")
    orders: list[dict[str, Any]] = []
    refunds: list[dict[str, Any]] = []
    channel_amounts = {"web": "10.10", "partner": "20.20", "store": "30.30"}
    for day in range(14):
        for channel, base_amount in channel_amounts.items():
            for position in range(2):
                created = WINDOW_START + timedelta(days=day, hours=12 * position)
                order_id = f"o-{day:02d}-{channel}-{position}"
                orders.append({
                    "order_id": order_id, "channel": channel,
                    "created_at": _iso(created), "available_at": _iso(created),
                    "amount": _money(Decimal(base_amount) + Decimal(position) / 100),
                    "currency": "USD",
                })
                # Three ordinary refunds per week; in the changed input the
                # partner channel also has first-order refunds on days 9-14.
                ordinary = day in {0, 7} and position == 0
                additional = variant == "anomaly" and day >= 8 and channel == "partner" and position == 0
                if ordinary or additional:
                    event = {
                        "refund_event_id": f"r-{order_id}", "order_id": order_id,
                        "occurred_at": _iso(created + timedelta(hours=6)),
                        "available_at": _iso(created + timedelta(hours=7)),
                        "amount": "2.50", "currency": "USD",
                    }
                    refunds.append(event)
                    if channel == "web":
                        refunds.append(dict(event))  # repeated delivery, same ID
                        refunds.append({
                            **event, "refund_event_id": f"partial-{order_id}",
                            "occurred_at": _iso(created + timedelta(hours=8)),
                            "available_at": _iso(created + timedelta(hours=9)),
                            "amount": "1.25",
                        })
    # A later event on a current-window order demonstrates the explicit
    # availability cutoff; it is absent at the default as_of.
    refunds.append({
        "refund_event_id": "late-current-web", "order_id": "o-13-web-1",
        "occurred_at": "2025-01-15T08:00:00Z",
        "available_at": "2025-01-15T09:00:00Z", "amount": "1.00", "currency": "USD",
    })
    return orders, refunds


class CommerceAdapter:
    @property
    def ref(self):
        return "src_" + digest(self._catalog_body()) + "@3"
    data_contract = "commerce@1"
    privacy = "public"

    def __init__(self, variant: str | None = None):
        # Variant selection is trusted server configuration, never a tool arg.
        fixture = json.loads((Path(__file__).resolve().parents[2] / "eval/fixture_config.json").read_text())
        selection = fixture["default_source"] if variant is None else next(
            (ref for ref, label in fixture["sources"].items() if label == variant), "")
        if not selection:
            raise ValueError("Unknown synthetic source configuration")
        orders, refunds = _records(fixture["sources"][selection])
        self._orders = tuple(orders)
        self._refunds = tuple(refunds)
        self.tools = {
            "compare@2": ToolSpec(side_effects="none", retry="new_action_allowed", 
                ref="compare@2", capability="commerce.compare",
                description="Compare the task's fixed current/baseline order cohorts, using its refund availability cutoff.",
                input_schema=EMPTY_ARGS,
            ),
            "breakdown@2": ToolSpec(side_effects="none", retry="new_action_allowed", 
                ref="breakdown@2", capability="commerce.breakdown",
                description="Break down the same fixed task cohorts by channel, with distinct refunded-order counts.",
                input_schema=CHANNEL_ARGS,
            ),
            "check_coverage@2": ToolSpec(side_effects="none", retry="new_action_allowed", 
                ref="check_coverage@2", capability="commerce.coverage",
                description="Check declared synthetic source coverage and query cohort sizes; an empty cohort is not a query error.",
                input_schema=EMPTY_ARGS,
            ),
        }

    def knowledge_cutoff(self, parameters):
        self.validate_task(parameters)
        return parameters["as_of"]

    def capabilities(self) -> dict[str, bool]:
        return {"live": True, "frozen": False, "cancel": False,
                "commerce.compare": True, "commerce.breakdown": True, "commerce.coverage": True}

    def catalog(self) -> dict[str, Any]:
        body = self._catalog_body()
        return {"source_ref": "src_" + digest(body) + "@3", **body}

    def _catalog_body(self):
        return {
            "data_contract": self.data_contract,
            "source_records_sha256": digest(json.loads(json.dumps(
                [self._orders, self._refunds], default=str, sort_keys=True))),
            "source_configuration_sha256": digest(json.loads((Path(__file__).resolve().parents[2] / "eval/fixture_config.json").read_text())),
            "privacy": self.privacy, "capabilities": self.capabilities(),
            "entity_keys": {"orders": ["order_id"], "refund_events": ["refund_event_id"]},
            "time_zone": "UTC", "intervals": "start <= created_at < end",
            "source_order_coverage": {"start": _iso(WINDOW_START), "end": _iso(WINDOW_END)},
            "source_refund_observation_end": _iso(OBSERVATION_END),
            "refund_cutoff": "occurred_at < as_of AND available_at < as_of",
            "order_cutoff": "created_at < as_of AND available_at < as_of",
            "reference_time": {"selector": "window", "windows": {
                "current": {"start": "current_start", "end": "current_end"},
                "baseline": {"start": "baseline_start", "end": "baseline_end"}}},
            "derivation_policy": {"operations": {op: {"input_units": units,
                "decimal_places": 8, "rounding": "ROUND_HALF_UP", "tolerance": "0.000000005"}
                for op, units in {"subtract": ["orders", "USD"],
                    "ratio": ["orders", "USD", "ratio"], "pct_change": ["orders", "USD", "ratio"],
                    "pp_change": ["ratio"]}.items()}},
            "metrics": {
                "orders": "Unique order IDs in the requested creation cohort, available before as_of.",
                "refunded_orders": "Distinct cohort order IDs with an available refund event before as_of.",
                "refund_rate": "refunded_orders / orders; Decimal rounded to 8 places, null for zero orders.",
                "order_amount": "Sum order amounts once per order; USD Decimal, two places.",
                "refund_amount": "Sum each distinct refund_event_id once; several partial refunds may belong to one order.",
            },
            "parameters": sorted(PARAMETERS),
            "limitations": ["Invented fixture records, not production evidence.",
                            "Live source only; a fixed fixture is not certified frozen execution.",
                            "No arbitrary SQL, dimensions, currencies or calendar inference."],
        }

    def validate_task(self, parameters: dict[str, Any]) -> None:
        if not isinstance(parameters, dict) or set(parameters) != PARAMETERS:
            raise WorkbenchError("invalid_parameters", "Exactly the documented task window/cutoff fields are required", 422)
        p = {key: _utc(value) for key, value in parameters.items()}
        for window in ("current", "baseline"):
            start, end = p[f"{window}_start"], p[f"{window}_end"]
            if not WINDOW_START <= start < end <= WINDOW_END:
                raise WorkbenchError("out_of_scope", "Window lies outside declared source coverage or is empty", 422)
            if end > p["as_of"]:
                raise WorkbenchError("out_of_scope", "Task cannot read beyond its as_of cutoff", 422)
        if p["baseline_end"] > p["current_start"]:
            raise WorkbenchError("invalid_parameters", "Baseline must precede current window without overlap", 422)
        if p["as_of"] > OBSERVATION_END:
            raise WorkbenchError("out_of_scope", "Refund observation cutoff exceeds declared source coverage", 422)

    def validate_call(self, tool_ref: str, args: dict[str, Any], parameters: dict[str, Any]) -> None:
        self.validate_task(parameters)
        if tool_ref not in self.tools:
            raise WorkbenchError("unknown_tool", "Tool is not registered", 422)
        expected = {"dimension": "channel"} if tool_ref == "breakdown@2" else {}
        if not isinstance(args, dict) or args != expected:
            raise WorkbenchError("invalid_parameters", "Tool parameters cannot change task windows, cutoff, baseline or dimension", 422)

    def _available_refunds(self, cutoff: datetime) -> list[dict[str, Any]]:
        unique: dict[str, dict[str, Any]] = {}
        for event in self._refunds:
            event_id = event["refund_event_id"]
            previous = unique.get(event_id)
            if previous is not None and previous != event:
                raise WorkbenchError("data_conflict", "Conflicting duplicate refund-event identity", 422)
            unique[event_id] = event
        return [event for event in unique.values()
                if _utc(event["occurred_at"]) < cutoff and _utc(event["available_at"]) < cutoff]

    def _cohort(self, window: str, p: dict[str, datetime]) -> list[dict[str, Any]]:
        selected = [order for order in self._orders
                    if p[f"{window}_start"] <= _utc(order["created_at"]) < p[f"{window}_end"]
                    and _utc(order["available_at"]) < p["as_of"]]
        if len({r["order_id"] for r in selected}) != len(selected):
            raise WorkbenchError("data_conflict", "Duplicate order identity", 422)
        return selected

    def _metrics(self, orders: list[dict[str, Any]], refunds: list[dict[str, Any]]) -> dict[str, Any]:
        ids = {order["order_id"] for order in orders}
        associated = [event for event in refunds if event["order_id"] in ids]
        if any(r["currency"] != "USD" for r in orders + associated):
            raise WorkbenchError("incompatible_units", "Mixed currencies require a separate conversion contract", 422)
        refunded = len({event["order_id"] for event in associated})
        return {
            "orders": len(ids), "refunded_orders": refunded, "refund_rate": _rate(refunded, len(ids)),
            "order_amount": _money(sum((Decimal(r["amount"]) for r in orders), Decimal(0))),
            "refund_amount": _money(sum((Decimal(r["amount"]) for r in associated), Decimal(0))),
        }

    def execute(self, tool_ref: str, args: dict[str, Any], parameters: dict[str, Any]) -> TableResult:
        self.validate_call(tool_ref, args, parameters)
        p = {key: _utc(value) for key, value in parameters.items()}
        refunds = self._available_refunds(p["as_of"])
        rows: list[dict[str, Any]] = []
        for window in ("current", "baseline"):
            cohort = self._cohort(window, p)
            if tool_ref == "check_coverage@2":
                rows.append({"window": window, "covered": True, "orders": len(cohort),
                             "coverage_basis": "declared exhaustive synthetic fixture interval"})
            elif tool_ref == "breakdown@2":
                for channel in ("partner", "store", "web"):
                    rows.append({"window": window, "channel": channel,
                                 **self._metrics([r for r in cohort if r["channel"] == channel], refunds)})
            else:
                rows.append({"window": window, **self._metrics(cohort, refunds)})
        coverage = tool_ref == "check_coverage@2"
        return TableResult(
            rows=rows,
            units={"window": "label", "covered": "boolean", "orders": "orders", "coverage_basis": "text"}
            if coverage else {k: v for k, v in UNITS.items() if k != "channel" or tool_ref == "breakdown@2"},
            time_range={key: _iso(value) for key, value in p.items()},
            entity_keys=["window", "channel"] if tool_ref == "breakdown@2" else ["window"],
            completeness="complete_for_query", truncated=False,
            warnings=["Synthetic data; live consistency only. Refund rate is a ratio, not a percentage."],
            provenance={
                "source_ref": self.ref, "data_contract": self.data_contract,
                "source_content_sha256": digest({"orders": self._orders, "refunds": self._refunds}),
                "definition_version": "commerce_metrics@1", "origin": "independent public synthetic generator",
                "as_of_semantics": "exclusive event and availability cutoff",
                "refund_deduplication": "refund_event_id; numerator distinct order_id",
            },
        )


def demo_skill() -> SkillRelease:
    return SkillRelease(
        ref="public_commerce_review@3", data_contract="commerce@1", privacy="public",
        tools=("compare@2", "breakdown@2", "check_coverage@2"),
        instructions=(
            "Investigate the requested current order cohort against the specified baseline. "
            "Read the metric catalog and compare the fixed windows. Use the evidence to decide "
            "whether channel detail or coverage checks are needed; do not execute a ritual list "
            "of every tool. Preserve the user's baseline and as_of. Report each cohort's orders, "
            "distinct refunded orders and refund rate with evidence. Rates are ratios; state "
            "their unit before discussing changes. Separate observed concentration from causes. "
            "If the comparison is unchanged, say so; do not invent a change. Evidence lacks "
            "production meaning because these are independently invented example records."
        ),
    )
