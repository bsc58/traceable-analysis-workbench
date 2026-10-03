"""P1.5 termination invariants and persisted system budgets."""
import pytest
from pydantic import ValidationError
from sqlalchemy import update

from analysis_agent.contracts import Registry, Report, RunRequest
from analysis_agent.runtime import Workbench
from analysis_agent.storage import Objects, runs
from database_guard import reopen_test_store
from test_runtime import harness, real_store, valid_report, assert_error


@pytest.mark.parametrize('state', ['SUCCEEDED', 'FAILED', 'CANCELLED'])
def test_terminal_calls_and_reports_never_append_events_or_objects(harness, state):
    run_id = harness.create()
    item = harness.call(run_id)
    report = valid_report(item)
    harness.workbench.finalize(run_id, report)
    if state != 'SUCCEEDED':
        # Fixture setup only: P1 does not claim to implement cancel/fail commands.
        with harness.store.engine.begin() as c:
            c.execute(update(runs).where(runs.c.id == run_id).values(state=state))
    before = harness.row(run_id)
    events = harness.event_rows(run_id)
    objects = set(harness.objects.root.iterdir())
    assert harness.call(run_id) == item  # Accepted action idempotent read is the sole exception.
    assert_error('invalid_state', harness.call, run_id, key='new-key')
    assert_error('invalid_state', harness.call, run_id, args={'entity': 'beta', 'day': '2026-01-02'})
    assert_error('invalid_state', harness.workbench.call, run_id, 'missing-tool@9', {}, 'other-key', 'No dispatch')
    assert_error('invalid_state', harness.workbench.finalize, run_id, report)
    assert_error('invalid_state', harness.workbench.finalize, run_id, report.model_copy(update={'title': 'Another report'}))
    assert_error('invalid_state', harness.workbench.reconcile, run_id, older_than_seconds=0)
    attempt_id = harness.workbench.reopen(run_id)['attempts'][0]['id']
    assert_error('invalid_state', harness.workbench.resolve, run_id, attempt_id, actor='operator', reason='Terminal must not change')
    assert harness.row(run_id) == before
    assert harness.event_rows(run_id) == events
    assert set(harness.objects.root.iterdir()) == objects
    assert harness.adapter.execute_calls == 1


def test_zero_tool_insufficient_evidence_can_close_without_claiming_coverage(harness):
    run_id = harness.create()
    report = Report(result_status='insufficient_evidence', title='No usable evidence available', facts=[], limitations=['No source query was made'])
    result = harness.workbench.finalize(run_id, report)
    assert result['validation']['status'] == 'warning'
    assert result['validation']['required_fact_coverage'] == {
        'required': [], 'covered': [], 'exception': 'zero_tool_insufficient_evidence'}
    assert result['report']['facts'] == []
    row = harness.row(run_id)
    assert row['state'] == 'SUCCEEDED' and row['tool_calls'] == 0 and row['report_attempts'] == 1
    assert [e['kind'] for e in harness.event_rows(run_id)] == ['RUN_ADMITTED', 'SKILL_DELIVERED', 'REPORT_PUBLISHED']


@pytest.mark.parametrize('status', ['complete', 'partial'])
def test_zero_tool_empty_report_cannot_claim_completion(harness, status):
    run_id = harness.create()
    result = harness.workbench.finalize(run_id, Report(result_status=status, title='Empty candidate', facts=[]))
    assert result['validation']['status'] == 'blocked'
    if status == 'complete':
        assert 'complete_report_needs_facts' in [e['code'] for e in result['validation']['errors']]
    assert harness.row(run_id)['state'] == 'ADMITTED'
    assert harness.row(run_id)['report_hash'] is None


def test_report_budget_counts_blocked_candidates_and_survives_reopen(harness):
    run_id = harness.create()
    report = valid_report(harness.call(run_id))
    wrong = report.model_copy(update={'facts': [report.facts[0].model_copy(update={'value': 999})]})
    assert harness.row(run_id)['manifest']['budget']['report_attempts'] == 3
    for index in range(3):
        result = harness.workbench.finalize(run_id, wrong.model_copy(update={'title': f'Incorrect candidate {index}'}))
        assert result['validation']['status'] == 'blocked'
        assert harness.row(run_id)['report_attempts'] == index + 1
    before_events = harness.event_rows(run_id)
    before_objects = set(harness.objects.root.iterdir())
    store = reopen_test_store(harness.store)
    try:
        w = Workbench(store, Objects(harness.objects.root), harness.registry, harness.actor, {harness.project: [harness.adapter.ref]})
        assert w.reopen(run_id)['run']['report_attempts'] == 3
        assert_error('invalid_state', w.finalize, run_id, report)
    finally:
        store.engine.dispose()
    assert harness.event_rows(run_id) == before_events
    assert set(harness.objects.root.iterdir()) == before_objects
    assert harness.row(run_id)['report_attempts'] == 3


def test_custom_report_budget_is_a_bound_system_setting(harness):
    run_id = harness.create(report_budget=1)
    report = valid_report(harness.call(run_id))
    wrong = report.model_copy(update={'facts': [report.facts[0].model_copy(update={'unit': 'wrong'})]})
    assert harness.workbench.finalize(run_id, wrong)['validation']['status'] == 'blocked'
    assert_error('invalid_state', harness.workbench.finalize, run_id, report)
    assert harness.row(run_id)['manifest']['budget']['report_attempts'] == 1


@pytest.mark.parametrize('value', [0, 21])
def test_report_budget_contract_rejects_invalid_limits(harness, value):
    with pytest.raises(ValidationError):
        RunRequest(**{**harness.request.model_dump(), 'report_budget': value})
