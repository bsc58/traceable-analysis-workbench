"""P1.3 synthetic outcome classes; no source database or paid provider calls."""
import json

import pytest
from sqlalchemy import select

from analysis_agent.contracts import TableResult, WorkbenchError, canonical
from analysis_agent.storage import attempts, evidence
from test_runtime import harness, real_store, valid_report, assert_error


def one_attempt(harness, run_id):
    with harness.store.engine.connect() as c:
        return dict(c.execute(select(attempts).where(attempts.c.run_id == run_id)).mappings().one())


@pytest.mark.parametrize('failure,code', [
    (RuntimeError('secret://database/password'), 'tool_failed'),
    (WorkbenchError('source_query_failed', 'secret source statement'), 'source_query_failed'),
    (WorkbenchError('incompatible', 'secret private path'), 'incompatible'),
])
def test_readonly_errors_are_durable_safe_and_new_actions_can_finish(harness, monkeypatch, failure, code):
    run_id = harness.create()
    execute = harness.adapter.execute
    def fail(*args):
        raise failure
    monkeypatch.setattr(harness.adapter, 'execute', fail)
    assert_error(code, harness.call, run_id)
    a = one_attempt(harness, run_id)
    assert a['state'] == 'FAILED' and a['error_code'] == code
    error = harness.objects.get(a['error_object'])
    assert 'secret' not in json.dumps(error) and code in json.dumps(error)
    assert harness.row(run_id)['state'] == 'RUNNING'
    assert harness.row(run_id)['tool_calls'] == 1
    assert harness.event_rows(run_id)[-1]['kind'] == 'TOOL_FAILED'
    assert harness.workbench.reopen(run_id)['attempts'][0]['error'] == error
    monkeypatch.setattr(harness.adapter, 'execute', execute)
    item = harness.call(run_id, key='explicit-new-action')
    assert harness.workbench.finalize(run_id, valid_report(item))['validation']['status'] == 'valid'
    assert harness.row(run_id)['tool_calls'] == 2


def test_oversized_result_fails_without_becoming_evidence(harness, monkeypatch):
    spec = harness.adapter.tools['read_counter@2']
    harness.adapter.tools[spec.ref] = spec.model_copy(update={'max_result_bytes': 700})
    run_id = harness.create()
    execute = harness.adapter.execute
    def oversized(*args):
        result = execute(*args)
        return result.model_copy(update={'warnings': ['x' * 2000]})
    monkeypatch.setattr(harness.adapter, 'execute', oversized)
    assert_error('result_too_large', harness.call, run_id)
    a = one_attempt(harness, run_id)
    assert a['state'] == 'FAILED' and a['evidence_id'] is None
    assert a['error_code'] == 'result_too_large'
    assert harness.row(run_id)['state'] == 'RUNNING'
    monkeypatch.setattr(harness.adapter, 'execute', execute)
    assert harness.call(run_id, key='smaller-result')['status'] == 'ok'


def test_predispatch_rejection_has_no_attempt_or_budget_cost(harness):
    run_id = harness.create()
    assert_error('invalid_parameters', harness.call, run_id, args={'entity': 'beta', 'day': '2026-01-02'})
    assert harness.workbench.reopen(run_id)['attempts'] == []
    assert harness.row(run_id)['tool_calls'] == 0
    assert harness.event_rows(run_id)[-1]['kind'] == 'TOOL_REJECTED'


@pytest.mark.parametrize('failure_site', ['binding_check', 'accept_event'])
def test_acceptance_rollback_is_classified_in_a_separate_transaction(harness, monkeypatch, failure_site):
    run_id = harness.create()
    check = harness.workbench._current
    event = harness.store.event
    calls = 0
    def changed(run):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise WorkbenchError('version_changed', 'Pinned binding changed')
        return check(run)
    def failed_event(c, run_id, kind, payload):
        if kind == 'EVIDENCE_ACCEPTED':
            raise RuntimeError('secret database commit details')
        return event(c, run_id, kind, payload)
    if failure_site == 'binding_check':
        monkeypatch.setattr(harness.workbench, '_current', changed)
    else:
        monkeypatch.setattr(harness.store, 'event', failed_event)
    with pytest.raises(WorkbenchError):
        harness.call(run_id)
    a = one_attempt(harness, run_id)
    assert a['state'] == 'REJECTED_AFTER_EXECUTION' and a['evidence_id'] is None
    assert harness.row(run_id)['state'] == 'RUNNING'
    audit = harness.event_rows(run_id)
    assert [e['kind'] for e in audit] == ['RUN_ADMITTED', 'SKILL_DELIVERED', 'TOOL_DISPATCHED', 'TOOL_REJECTED_AFTER_EXECUTION']
    result_hash = audit[-1]['payload']['result_object']
    unaccepted = harness.objects.get(result_hash)
    assert_error('invalid_evidence', harness.workbench.read_evidence, run_id, unaccepted['evidence_id'])
    assert harness.workbench.finalize(run_id, valid_report(unaccepted))['validation']['status'] == 'blocked'
    monkeypatch.setattr(harness.workbench, '_current', check)
    monkeypatch.setattr(harness.store, 'event', event)
    item = harness.call(run_id, key='after-rejection')
    assert harness.workbench.finalize(run_id, valid_report(item))['validation']['status'] == 'valid'
    assert sum(e['kind'] == 'EVIDENCE_ACCEPTED' for e in harness.event_rows(run_id)) == 1


def test_external_uncertainty_waits_for_operator(harness):
    spec = harness.adapter.tools['read_counter@2']
    harness.adapter.tools[spec.ref] = spec.model_copy(update={'side_effects': 'external', 'retry': 'never'})
    run_id = harness.create()
    harness.adapter.fail_execution = True
    assert_error('outcome_unknown', harness.call, run_id)
    a = one_attempt(harness, run_id)
    assert a['state'] == 'UNKNOWN' and a['outcome_class'] == 'uncertain'
    assert harness.row(run_id)['state'] == 'WAITING_RECONCILIATION'
    assert harness.row(run_id)['tool_calls'] == 1
    assert_error('invalid_state', harness.call, run_id, key='unapproved-retry')


def test_process_like_crash_stays_dispatched_until_explicit_reconcile(harness, monkeypatch):
    class Crash(BaseException): pass
    run_id = harness.create()
    def crash(*args): raise Crash()
    monkeypatch.setattr(harness.adapter, 'execute', crash)
    with pytest.raises(Crash): harness.call(run_id)
    assert one_attempt(harness, run_id)['state'] == 'DISPATCHED'
    assert harness.row(run_id)['state'] == 'RUNNING'
    assert [e['kind'] for e in harness.workbench.reopen(run_id)['events']] == ['RUN_ADMITTED', 'SKILL_DELIVERED', 'TOOL_DISPATCHED']
    assert_error('busy', harness.call, run_id, key='unsafe-retry')


def test_error_cas_failure_does_not_claim_durable_failure(harness, monkeypatch):
    run_id = harness.create()
    harness.adapter.fail_execution = True
    put = harness.objects.put
    def unavailable(value):
        if isinstance(value, dict) and value.get('schema_version') == 'tool_error@1':
            raise OSError('secret filesystem detail')
        return put(value)
    monkeypatch.setattr(harness.objects, 'put', unavailable)
    with pytest.raises(WorkbenchError): harness.call(run_id)
    assert one_attempt(harness, run_id)['state'] == 'DISPATCHED'
    assert harness.event_rows(run_id)[-1]['kind'] == 'TOOL_DISPATCHED'


def test_never_retry_policy_refuses_equivalent_new_action(harness):
    spec = harness.adapter.tools['read_counter@2']
    harness.adapter.tools[spec.ref] = spec.model_copy(update={'retry': 'never'})
    run_id = harness.create()
    harness.adapter.fail_execution = True
    assert_error('tool_failed', harness.call, run_id)
    harness.adapter.fail_execution = False
    with pytest.raises(WorkbenchError): harness.call(run_id, key='same-decision-new-key')
    assert harness.row(run_id)['tool_calls'] == 1
    assert harness.adapter.execute_calls == 1
    assert harness.call(run_id, key='different-query', args={'entity': 'alpha', 'day': '2026-01-03'})['status'] == 'ok'


def test_external_acceptance_failure_also_requires_resolution(harness, monkeypatch):
    spec = harness.adapter.tools['read_counter@2']
    harness.adapter.tools[spec.ref] = spec.model_copy(update={'side_effects': 'external', 'retry': 'never'})
    run_id = harness.create()
    event = harness.store.event
    def failed_event(c, run_id, kind, payload):
        if kind == 'EVIDENCE_ACCEPTED':
            raise RuntimeError('simulated uncertain commit')
        return event(c, run_id, kind, payload)
    monkeypatch.setattr(harness.store, 'event', failed_event)
    assert_error('outcome_unknown', harness.call, run_id)
    a = one_attempt(harness, run_id)
    assert a['state'] == 'UNKNOWN' and a['evidence_id'] is None
    assert harness.row(run_id)['state'] == 'WAITING_RECONCILIATION'
    audit = harness.event_rows(run_id)[-1]
    assert audit['kind'] == 'TOOL_OUTCOME_UNCERTAIN'
    result = harness.objects.get(audit['payload']['result_object'])
    assert_error('invalid_evidence', harness.workbench.read_evidence, run_id, result['evidence_id'])
