from decimal import Decimal

import pytest

from analysis_agent.contracts import Registry, RunRequest, WorkbenchError
from analysis_agent.demo import CommerceAdapter, default_parameters, demo_skill


def test_distinct_orders_partial_refunds_and_money_are_not_join_inflated():
    result = CommerceAdapter().execute("compare@2", {}, default_parameters())
    current, baseline = result.rows
    assert (current["orders"], baseline["orders"]) == (42, 42)
    assert (current["refunded_orders"], baseline["refunded_orders"]) == (9, 3)
    assert current["order_amount"] == baseline["order_amount"] == "848.61"
    assert current["refund_amount"] == "23.75"
    assert baseline["refund_amount"] == "8.75"
    assert current["refund_rate"] == "0.21428571"
    assert baseline["refund_rate"] == "0.07142857"
    assert result.units["refund_rate"] == "ratio"
    assert result.truncated is False


def test_counterfactual_case_only_partner_refund_concentration_changes():
    params = default_parameters()
    normal_adapter = CommerceAdapter("normal")
    assert normal_adapter.ref.startswith("src_") and normal_adapter.ref.endswith("@3")
    assert normal_adapter.ref != CommerceAdapter().ref
    assert "fixture_variant" not in normal_adapter.catalog()
    normal = normal_adapter.execute("compare@2", {}, params)
    assert normal.rows[0]["refunded_orders"] == normal.rows[1]["refunded_orders"] == 3
    changed = CommerceAdapter().execute("breakdown@2", {"dimension": "channel"}, params)
    lookup = {(r["window"], r["channel"]): r for r in changed.rows}
    assert lookup["current", "partner"]["refunded_orders"] == 7
    assert lookup["baseline", "partner"]["refunded_orders"] == 1
    for channel in ("web", "store"):
        assert lookup["current", channel]["refunded_orders"] == lookup["baseline", channel]["refunded_orders"] == 1
    assert sum(Decimal(r["order_amount"]) for r in changed.rows if r["window"] == "current") == Decimal("848.61")


def test_half_open_windows_and_zero_denominator_are_explicit():
    adapter = CommerceAdapter()
    params = default_parameters() | {
        "baseline_start": "2025-01-01T00:00:00Z", "baseline_end": "2025-01-01T12:00:00Z",
        "current_start": "2025-01-01T12:00:00Z", "current_end": "2025-01-02T00:00:00Z",
    }
    current, baseline = adapter.execute("compare@2", {}, params).rows
    assert current["orders"] == baseline["orders"] == 3
    assert baseline["refunded_orders"] == 3
    assert current["refunded_orders"] == 0
    empty = params | {"current_start": "2025-01-01T13:00:00Z", "current_end": "2025-01-01T14:00:00Z"}
    current = adapter.execute("compare@2", {}, empty).rows[0]
    assert current["orders"] == 0 and current["refund_rate"] is None


def test_refund_must_be_available_before_exclusive_cutoff():
    adapter = CommerceAdapter()
    before = default_parameters() | {"as_of": "2025-01-15T08:30:00Z"}
    equal = default_parameters() | {"as_of": "2025-01-15T09:00:00Z"}
    after = default_parameters() | {"as_of": "2025-01-15T09:00:01Z"}
    assert adapter.execute("compare@2", {}, before).rows[0]["refunded_orders"] == 9
    assert adapter.execute("compare@2", {}, equal).rows[0]["refunded_orders"] == 9
    assert adapter.execute("compare@2", {}, after).rows[0]["refunded_orders"] == 10


@pytest.mark.parametrize("args", [{"current_start": "2025-01-09T00:00:00Z"}, {"baseline": "other"}, {"variant": "normal"}])
def test_call_cannot_rewrite_request_scope(args):
    with pytest.raises(WorkbenchError, match="cannot change"):
        CommerceAdapter().execute("compare@2", args, default_parameters())


@pytest.mark.parametrize("override", [
    {"baseline_start": "2024-12-31T00:00:00Z"},
    {"baseline_end": "2025-01-09T00:00:00Z"},
    {"current_end": "2025-01-15T00:00:00"},
    {"current_end": "2025-01-15T01:00:00+01:00"},
    {"as_of": "2025-01-14T00:00:00Z"},
    {"as_of": "2025-01-17T00:00:00Z"},
    {"unexpected": True},
])
def test_invalid_time_scope_and_extra_fields_rejected(override):
    with pytest.raises(WorkbenchError):
        CommerceAdapter().validate_task(default_parameters() | override)


def test_live_only_and_skill_contract_are_honest():
    adapter = CommerceAdapter()
    registry = Registry([adapter], [demo_skill()])
    request = RunRequest(project_id="demo", source_ref=adapter.ref, skill_ref=demo_skill().ref,
                         question="Investigate refund changes", parameters=default_parameters(), consistency="frozen")
    with pytest.raises(WorkbenchError) as exc:
        registry.resolve(request)
    assert exc.value.code == "unsupported"
    assert adapter.catalog()["capabilities"]["frozen"] is False
    coverage = adapter.execute("check_coverage@2", {}, default_parameters())
    assert all(r["covered"] and r["orders"] == 42 for r in coverage.rows)


def test_conflicting_event_ids_and_mixed_units_fail_explicitly():
    adapter = CommerceAdapter()
    adapter._refunds += ({**adapter._refunds[0], "amount": "99.99"},)
    with pytest.raises(WorkbenchError) as exc:
        adapter.execute("compare@2", {}, default_parameters())
    assert exc.value.code == "data_conflict"
    adapter = CommerceAdapter()
    adapter._orders = ({**adapter._orders[0], "currency": "EUR"}, *adapter._orders[1:])
    with pytest.raises(WorkbenchError) as exc:
        adapter.execute("compare@2", {}, default_parameters())
    assert exc.value.code == "incompatible_units"
