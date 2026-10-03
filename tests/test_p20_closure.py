import pytest
from pydantic import ValidationError
from analysis_agent.contracts import Registry, Report, ToolSpec
from test_runtime import harness, real_store, assert_error, valid_report

@pytest.mark.parametrize('field', ['side_effects','retry'])
def test_registry_rejects_missing_policy(harness, field):
    data=harness.adapter.tools['read_counter@2'].model_dump(); del data[field]
    harness.adapter.tools['read_counter@2']=ToolSpec.model_construct(**data)
    with pytest.raises(ValidationError): Registry([harness.adapter],list(harness.registry.skills.values()))

def check_closed(harness, run_id, report):
    before=harness.event_rows(run_id); objects=set(harness.objects.root.iterdir())
    assert_error('invalid_state',harness.call,run_id,key='closed')
    assert_error('invalid_state',harness.workbench.finalize,run_id,report)
    assert harness.event_rows(run_id)==before
    assert set(harness.objects.root.iterdir())==objects

def test_last_blocked_report_fails_atomically(harness):
    r=harness.create(report_budget=1); item=harness.call(r)
    report=valid_report(item).model_copy(update={'facts':[]})
    assert harness.workbench.finalize(r,report)['validation']['status']=='blocked'
    assert harness.row(r)['state']=='FAILED'
    events=harness.event_rows(r)
    assert [e['kind'] for e in events[-2:]]==['REPORT_BLOCKED','RUN_FAILED']
    assert events[-1]['payload']=={'reason':'report_budget_exhausted'}
    check_closed(harness,r,report)

@pytest.mark.parametrize('outcome',['FAILED','REJECTED_AFTER_EXECUTION','RESOLVED_FAILED'])
def test_all_failed_can_close_and_retains_attempt_ids(harness, monkeypatch, outcome):
    r=harness.create()
    if outcome=='FAILED':
        harness.adapter.fail_execution=True
        assert_error('tool_failed',harness.call,r)
    elif outcome=='REJECTED_AFTER_EXECUTION':
        def fail(point,ctx):
            if point=='accept_before_commit': raise RuntimeError('Synthetic rollback')
        harness.workbench._fault_hook=fail
        assert_error('acceptance_rejected',harness.call,r)
    else:
        class Crash(BaseException): pass
        def crash(point,ctx):
            if point=='after_intent_commit': raise Crash()
        harness.workbench._fault_hook=crash
        with pytest.raises(Crash): harness.call(r)
        harness.workbench._fault_hook=None
        harness.workbench.reconcile(r,older_than_seconds=0)
        a=harness.workbench.reopen(r)['attempts'][0]
        harness.workbench.resolve(r,a['id'],actor='tester',reason='Synthetic readonly crash')
    record=harness.workbench.reopen(r); a=record['attempts'][0]
    assert a['state']==outcome
    report=Report(result_status='insufficient_evidence',title='No accepted evidence',facts=[])
    result=harness.workbench.finalize(r,report)
    assert result['validation']['status']=='valid'
    assert result['validation']['failed_attempt_ids']==[a['id']]
    assert result['validation']['required_fact_coverage']['covered']==[]
    assert harness.row(r)['state']=='SUCCEEDED'
    check_closed(harness,r,report)
