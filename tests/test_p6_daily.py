"""Generic day ordering with synthetic entities, no private rules or source."""
import pytest
from sqlalchemy import select
from analysis_agent.contracts import DailyProtocol,Report,WorkbenchError,RunRequest
from analysis_agent.daily import record
from analysis_agent.storage import decisions,daily_sessions
from test_runtime import harness,real_store,assert_error


def setup(h,query=True):
    original=h.adapter.capabilities
    h.adapter.capabilities=lambda:{**original(),'daily_protocol':True}
    h.adapter.validate_task=lambda p:None
    h.adapter.knowledge_cutoff=lambda p:p['as_of']
    h.adapter.validate_call=lambda tool,args,p: (_ for _ in ()).throw(WorkbenchError('out_of_scope','future',403)) if args['day']>p['as_of'] else None
    request=h.request.model_copy(update={'parameters':{**h.request.parameters,'as_of':'2026-01-02'},'daily_protocol':DailyProtocol(dates=['2026-01-02','2026-01-03'],review_only=['2026-01-03'])})
    run=h.workbench.create(request,'daily')['run_id']
    if query:h.workbench.call(run,'read_counter@2',{'entity':'alpha','day':'2026-01-02'},'initial-read','Query current day first')
    return run


def decision(day='2026-01-02',state='new_entry'):
    return {'day':day,'reviewed':[{'entity':'alpha','reason':'Synthetic review'}],'selections':[{'entity':'alpha','state':state,'reason':'Synthetic judgment'}],'stopping_reason':'No additional supported evidence.'}


def test_future_hidden_until_atomic_decision_and_old_immutable(harness):
    run=setup(harness);w=harness.workbench
    assert_error('out_of_scope',w.call,run,'read_counter@2',{'entity':'alpha','day':'2026-01-03'},'future','future')
    assert w.effective_parameters(harness.row(run))['as_of']=='2026-01-02'
    first=w.submit_decision(run,decision());assert first['accepted']
    assert w.effective_parameters(harness.row(run))['as_of']=='2026-01-03'
    assert w.submit_decision(run,decision())['replayed']
    changed=decision();changed['stopping_reason']='changed'
    assert_error('immutable_decision',w.submit_decision,run,changed)
    w.call(run,'read_counter@2',{'entity':'alpha','day':'2026-01-03'},'now','now allowed')
    assert len(record(w,run)['decisions'])==1


def test_report_waits_for_decisions_review_day_refuses_new_entry(harness):
    run=setup(harness);w=harness.workbench
    report=Report(result_status='insufficient_evidence',title='Synthetic',facts=[])
    assert_error('daily_decision_required',w.finalize,run,report)
    w.submit_decision(run,decision());assert_error('review_only',w.submit_decision,run,decision('2026-01-03'))
    w.call(run,'read_counter@2',{'entity':'alpha','day':'2026-01-03'},'second-read','Query second day')
    w.submit_decision(run,decision('2026-01-03','right_censored'));w.finalize(run,report)
    assert w.reopen(run)['daily']['complete'] is True


@pytest.mark.parametrize('state',['continue','exit','unknown','right_censored'])
def test_states_preserved_without_business_thresholds(harness,state):
    run=setup(harness);harness.workbench.submit_decision(run,decision(state=state))
    assert record(harness.workbench,run)['decisions'][0]['selections'][0]['state']==state


def test_append_review_keeps_original_decision(harness):
    run=setup(harness);w=harness.workbench;w.submit_decision(run,decision());before=record(w,run)['decisions']
    w.append_decision_review(run,'2026-01-02','Later qualification')
    after=record(w,run);assert after['decisions']==before and after['reviews'][0]['text']=='Later qualification'


def test_transaction_failure_does_not_open_next_day(harness):
    run=setup(harness);w=harness.workbench
    def fail(point,**kwargs):
        if point=='daily_after_insert_before_commit':raise RuntimeError('crash')
    w.fault_hook=fail
    # Existing fault-injection hook is supplied through _checkpoint's configured callable.
    original=w._checkpoint;w._checkpoint=fail
    with pytest.raises(RuntimeError):w.submit_decision(run,decision())
    w._checkpoint=original
    assert w.effective_parameters(harness.row(run))['as_of']=='2026-01-02'
    assert record(w,run)['decisions']==[]


def test_future_decision_and_foreign_evidence_rejected(harness):
    run=setup(harness);w=harness.workbench
    assert_error('out_of_scope',w.submit_decision,run,decision('2026-01-03'))
    value=decision();value['reviewed'][0]['evidence_ids']=['foreign']
    assert_error('foreign_evidence',w.submit_decision,run,value)


def test_internal_provider_daily_loop_and_skill_inputs(harness,executor):
    import json
    from analysis_agent.providers import FixtureProvider
    from analysis_agent.contracts import DailyProtocol
    # Configure the same synthetic daily adapter but create a separate internal run.
    setup(harness)
    request=harness.request.model_copy(update={'parameters':{**harness.request.parameters,'as_of':'2026-01-02'},'daily_protocol':DailyProtocol(dates=['2026-01-02','2026-01-03'])})
    seen=[]
    def respond(payload):
        value=json.loads(payload['messages'][1]['content']);seen.append(value)
        if not value['daily']['complete']:
            day=value['daily']['current_date']
            if not any(e['time_range'].get('end')==day for e in value['accepted_evidence']):
                return {'action':'query','tool_ref':'read_counter@2','args':{'entity':'alpha','day':day},'explanation':'Query current day'}
            return {'action':'decision','decision':decision(value['daily']['current_date'],'unknown')}
        return {'action':'report','report':{'result_status':'insufficient_evidence','title':'Synthetic','facts':[]}}
    executor.provider=FixtureProvider(respond)
    run=executor.submit(request,'internal-daily',executor.config)['run_id'];result=executor.run('worker',run)
    assert result['run']['state']=='SUCCEEDED' and len(result['daily']['decisions'])==2
    assert [c['task']['parameters']['as_of'] for c in seen]==['2026-01-02','2026-01-02','2026-01-03','2026-01-03','2026-01-03']
    assert all(c['skill']['skill_digest']==result['run']['manifest']['bindings']['skill'] for c in seen)


from test_p4_executor import executor


def test_no_evidence_cannot_advance_but_empty_dated_query_can(harness):
    run=setup(harness,query=False);w=harness.workbench
    assert_error('daily_evidence_required',w.submit_decision,run,decision())
    assert w.effective_parameters(harness.row(run))['as_of']=='2026-01-02'
    original=harness.adapter.execute
    harness.adapter.execute=lambda *args:original(*args).model_copy(update={'rows':[]})
    w.call(run,'read_counter@2',{'entity':'alpha','day':'2026-01-02'},'empty-read','Source may have no rows')
    value=decision();value['selections']=[];value['reviewed']=[]
    assert w.submit_decision(run,value)['accepted']
