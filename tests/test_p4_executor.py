"""Synthetic internal runner checks, distinct from real model evaluation."""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import json
import uuid
import pytest
from sqlalchemy import delete, select, update
from analysis_agent.contracts import Report, WorkbenchError, digest
from analysis_agent.execution import BatchConfig, ExecutionConfig, Executor, Lease
from analysis_agent.providers import FixtureProvider, ProviderFailure
from analysis_agent.storage import budget_accounts, runs, run_execution, model_invocations
from test_runtime import harness, real_store, assert_error, valid_report


def responder(payload):
    context=json.loads(payload['messages'][1]['content'])
    items=context['accepted_evidence']
    if not items:return {'action':'query','tool_ref':'read_counter@2','args':{'entity':'alpha','day':'2026-01-02'},'explanation':'Read requested counter'}
    e=items[0]
    return {'action':'report','report':{'result_status':'complete','title':'Synthetic counter','facts':[{
        'schema_version':'fact@2','operation':'cell','inputs':[{'evidence_id':e['evidence_id'],'row':0,'column':'requests',
            'entity':{'entity':'alpha'},'time_range':e['time_range']}],'value':12,'unit':'count'}]}}


def batch_config():
    return BatchConfig(provider='fixture',model='fixture@1',returned_models=('fixture@1',),currency='fixture',
        input_per_million='1',output_per_million='1',pricing_source='fixture',pricing_date='2026-10-02',cost_micros=15000000)


@pytest.fixture
def executor(harness):
    e=Executor(harness.workbench,FixtureProvider(responder));account='test-'+uuid.uuid4().hex
    e.register_batch(account,batch_config());e.config=ExecutionConfig(provider='fixture',model='fixture@1',batch_account=account)
    yield e
    e.close()
    with harness.store.engine.begin() as c:c.execute(delete(budget_accounts).where(budget_accounts.c.id==account))


def submit(e,h,**kwargs):
    return e.submit(h.request,'internal-'+uuid.uuid4().hex,e.config.model_copy(update=kwargs))['run_id']


def expire(h,run):
    with h.store.engine.begin() as c:c.execute(update(runs).where(runs.c.id==run).values(lease_expires_at=0))


def test_full_internal_loop_binds_skill_and_reopens_without_provider(harness,executor):
    r=submit(executor,harness);result=executor.run('worker',r)
    assert result['run']['state']=='SUCCEEDED'
    assert result['run']['manifest']['execution']['driver']=='internal_runner'
    assert executor.provider.calls==2 and result['report']['validation']['status']=='valid'
    intents=[x for x in result['model_invocations'] if x['kind']=='INTENT']
    accepted=[x for x in result['model_invocations'] if x['kind']=='ACCEPTED']
    assert len(intents)==len(accepted)==2
    for row in intents:
        payload=harness.objects.get(row['payload']['input_object'])
        assert payload['skill_digest']==result['run']['manifest']['bindings']['skill']
        assert digest(payload['payload'])==row['payload']['input_digest']
        assert payload['skill_digest'] in payload['payload']['messages'][0]['content']
        assert row['payload']['fixture'] is True
    assert all(x['payload']['actual_model']=='fixture@1' and x['payload']['provider_request_id'] for x in accepted)
    executor.provider.complete=lambda _:pytest.fail('replay called a provider')
    assert harness.workbench.reopen(r)==result


def test_queue_only_one_owner_fifty_concurrent_rounds(harness,executor):
    for _ in range(50):
        run=submit(executor,harness)
        with ThreadPoolExecutor(max_workers=4) as pool:
            claimed=list(pool.map(lambda n:executor.claim('worker-'+str(n),run),range(4)))
        assert sum(x is not None for x in claimed)==1
        assert harness.row(run)['epoch']==1
        executor.cancel(run)


def test_stale_worker_cannot_submit_query_report_or_model_intent(harness,executor):
    r=submit(executor,harness);old=executor.claim('old',r);expire(harness,r);new=executor.claim('new',r)
    assert new.epoch==old.epoch+1
    assert_error('stale_worker',harness.workbench.call,r,'read_counter@2',{'entity':'alpha','day':'2026-01-02'},'stale','why',lease=old)
    assert_error('stale_worker',harness.workbench.finalize,r,Report(result_status='insufficient_evidence',title='No data',facts=[]),lease=old)
    assert_error('stale_worker',executor.context,old)
    assert_error('stale_worker',executor.heartbeat,old)
    assert harness.adapter.execute_calls==0
    assert executor.heartbeat_store.engine is not harness.store.engine


@pytest.mark.parametrize('failure',['disconnect','cas'])
def test_network_or_response_save_failure_retains_reservation(harness,executor,monkeypatch,failure):
    r=submit(executor,harness);lease=executor.claim('one',r)
    if failure=='disconnect':
        def fail(_):raise ProviderFailure('provider_transport_unknown')
        executor.provider.complete=fail
    else:
        put=harness.objects.put
        def fail(value):
            if isinstance(value,dict) and 'response' in value:raise OSError('synthetic disk failure')
            return put(value)
        monkeypatch.setattr(harness.objects,'put',fail)
    with pytest.raises(WorkbenchError):executor.invoke(lease)
    assert harness.row(r)['state']=='WAITING_RECONCILIATION'
    with harness.store.engine.connect() as c:
        account=c.execute(select(budget_accounts).where(budget_accounts.c.id==executor.config.batch_account)).mappings().one()
        records=c.execute(select(model_invocations).where(model_invocations.c.run_id==r)).mappings().all()
    assert account['held']['output_tokens']==executor.config.max_output_tokens
    assert account['used']['output_tokens']==0
    assert [x['kind'] for x in records]==['INTENT','UNKNOWN']
    assert_error('unresolved_invocation',executor.resume,r)
    executor.resolve_unknown(r,records[0]['invocation_id'],'Abandon uncertain response; retain cost hold')
    assert executor.resume(r)['state']=='ADMITTED'
    with harness.store.engine.connect() as c:
        assert c.execute(select(budget_accounts.c.held).where(budget_accounts.c.id==executor.config.batch_account)).scalar_one()==account['held']


def test_saved_response_recovers_after_lease_takeover_without_repeat_network(harness,executor):
    class Crash(BaseException):pass
    r=submit(executor,harness);lease=executor.claim('old',r)
    def hook(point,ids):
        if point=='model_after_cas_before_accept':raise Crash()
    harness.workbench._fault_hook=hook
    with pytest.raises(Crash):executor.invoke(lease)
    harness.workbench._fault_hook=None;expire(harness,r);new=executor.claim('new',r)
    assert executor.recover(new) is True
    assert executor.provider.calls==1
    record=harness.workbench.reopen(r)
    assert [x['kind'] for x in record['model_invocations']]==['INTENT','ACCEPTED']
    assert record['model_invocations'][-1]['epoch']==new.epoch


def test_cancel_and_report_publish_race_is_atomic(harness,executor):
    for _ in range(12):
        r=submit(executor,harness);lease=executor.claim('worker',r)
        item=harness.workbench.call(r,'read_counter@2',{'entity':'alpha','day':'2026-01-02'},'read','required',lease=lease)
        def publish():
            try:return harness.workbench.finalize(r,valid_report(item),lease=lease)
            except WorkbenchError as exc:return exc.code
        with ThreadPoolExecutor(max_workers=2) as pool:
            a=pool.submit(publish);b=pool.submit(executor.cancel,r);a.result();b.result()
        row=harness.row(r);events=harness.event_rows(r)
        assert row['state'] in {'SUCCEEDED','CANCELLED'}
        assert sum(x['kind']=='REPORT_PUBLISHED' for x in events)+sum(x['kind']=='RUN_CANCELLED' for x in events)==1
        assert bool(row['report_hash'])==(row['state']=='SUCCEEDED')
        before=list(events);executor.cancel(r);assert harness.event_rows(r)==before


@pytest.mark.parametrize('state',['ADMITTED','FAILED','CANCELLED','SUCCEEDED'])
def test_resume_rejects_nonwhitelisted_states(harness,executor,state):
    r=submit(executor,harness)
    with harness.store.engine.begin() as c:c.execute(update(runs).where(runs.c.id==r).values(state=state))
    assert_error('resume_not_allowed',executor.resume,r)


@pytest.mark.parametrize('mode',['bad_args','tool_failure','unknown_action'])
def test_repeated_invalid_actions_and_tool_failures_stop_at_budget(harness,executor,mode):
    if mode=='tool_failure':harness.adapter.fail_execution=True
    executor.provider.responder=lambda _:({'action':'shell','command':'not available'} if mode=='unknown_action' else
        {'action':'query','tool_ref':'read_counter@2','args':{'entity':'alpha','day':'invalid' if mode=='bad_args' else '2026-01-02'}})
    r=submit(executor,harness,max_calls=3)
    assert_error('run_budget_exceeded',executor.run,'worker',r)
    assert executor.provider.calls==3
    assert harness.row(r)['state']=='FAILED'
    if mode=='bad_args':assert harness.adapter.execute_calls==0
    if mode=='tool_failure':assert harness.adapter.execute_calls==2 # task tool budget is two


def test_ten_percent_batch_reserve_boundary_and_no_network(harness,executor):
    r=submit(executor,harness);lease=executor.claim('worker',r)
    with harness.store.engine.begin() as c:
        c.execute(update(budget_accounts).where(budget_accounts.c.id==executor.config.batch_account).values(used={'input_tokens':1800000,'output_tokens':0,'cost_micros':0}))
    assert_error('batch_budget_low',executor.invoke,lease)
    assert executor.provider.calls==0


def test_insufficient_balance_halts_batch_and_never_retries(harness,executor):
    def fail(_):raise ProviderFailure('insufficient_balance')
    executor.provider.complete=fail;r=submit(executor,harness);lease=executor.claim('worker',r)
    assert_error('insufficient_balance',executor.invoke,lease)
    with harness.store.engine.connect() as c:
        assert c.execute(select(budget_accounts.c.halted).where(budget_accounts.c.id==executor.config.batch_account)).scalar_one()=='insufficient_balance'
    assert harness.row(r)['state']=='WAITING_RECONCILIATION'


def test_context_contains_only_bound_attachments_index_until_audited_fetch(harness,executor):
    from analysis_agent.contracts import Registry
    skill=next(iter(harness.registry.skills.values())).model_copy(update={'attachments':{'reference.md':'SYNTHETIC_ATTACHMENT_CANARY'}})
    harness.workbench.registry=Registry([harness.adapter],[skill])
    r=submit(executor,harness);lease=executor.claim('worker',r)
    payload,_=executor.context(lease)
    assert 'SYNTHETIC_ATTACHMENT_CANARY' not in json.dumps(payload)
    executor.provider.responder=lambda _:{'action':'skill_attachment','filename':'reference.md'}
    key,accepted=executor.invoke(lease);executor.apply(lease,key,accepted)
    payload,_=executor.context(lease)
    assert 'SYNTHETIC_ATTACHMENT_CANARY' in json.dumps(payload)
    assert any(e['kind']=='SKILL_DELIVERED' and e['payload']['attachment']=='reference.md' for e in harness.event_rows(r))


def test_wall_clock_budget_and_foreign_run_lease_are_enforced(harness,executor):
    from analysis_agent.execution import database_time
    r=submit(executor,harness,wall_seconds=1);lease=executor.claim('worker',r)
    assert_error('stale_worker',executor.context,replace(lease,run_id=harness.create()))
    with harness.store.engine.begin() as c:
        c.execute(update(run_execution).where(run_execution.c.run_id==r).values(started_at=database_time(c)-2))
    assert_error('wall_budget_exceeded',executor.context,lease)
    assert executor.provider.calls==0


def test_workbench_http_internal_creation_and_operator_cancel(harness,executor):
    from fastapi.testclient import TestClient
    from analysis_agent.api import create_app
    client=TestClient(create_app(harness.workbench,'z'*40,executor=executor,execution_config=executor.config))
    headers={'Authorization':'Bearer '+'z'*40,'Idempotency-Key':'http-internal'}
    response=client.post('/runs',json=harness.request.model_dump(),headers=headers)
    assert response.status_code==202
    r=response.json()['run_id']
    assert harness.row(r)['manifest']['execution']['driver']=='internal_runner'
    assert client.post(f'/runs/{r}/cancel',headers=headers).json()['state']=='CANCELLED'
    assert client.post(f'/runs/{r}/resume',headers=headers).json()['error']['code']=='resume_not_allowed'


def test_same_model_action_replay_does_not_repeat_tool(harness,executor):
    r=submit(executor,harness);lease=executor.claim('worker',r);key,accepted=executor.invoke(lease)
    first=executor.apply(lease,key,accepted);second=executor.apply(lease,key,accepted)
    assert first==second and harness.adapter.execute_calls==1


def test_response_from_displaced_worker_saved_but_not_accepted_by_old_epoch(harness,executor):
    r=submit(executor,harness);old=executor.claim('old',r);provider=executor.provider.complete;leases=[]
    def displaced(payload):
        expire(harness,r);leases.append(executor.claim('replacement',r));return provider(payload)
    executor.provider.complete=displaced
    assert_error('stale_worker',executor.invoke,old)
    assert [x['kind'] for x in harness.workbench.reopen(r)['model_invocations']]==['INTENT']
    assert executor.recover(leases[0])
    assert [x['kind'] for x in harness.workbench.reopen(r)['model_invocations']]==['INTENT','ACCEPTED']


def test_displaced_tool_intent_requires_operator_reconciliation(harness,executor):
    class Crash(BaseException):pass
    r=submit(executor,harness);old=executor.claim('old',r)
    def crash(point,identifiers):
        if point=='after_intent_commit':raise Crash()
    harness.workbench._fault_hook=crash
    with pytest.raises(Crash):harness.workbench.call(r,'read_counter@2',{'entity':'alpha','day':'2026-01-02'},'pending','required',lease=old)
    harness.workbench._fault_hook=None;expire(harness,r);new=executor.claim('replacement',r)
    assert executor.recover(new) is False
    assert harness.row(r)['state']=='WAITING_RECONCILIATION'
    record=harness.workbench.reopen(r)
    assert record['attempts'][0]['state']=='UNKNOWN'
    assert harness.adapter.execute_calls==0


def test_internal_wire_contract_excludes_legacy_fact_format(harness,executor):
    r=submit(executor,harness);lease=executor.claim('worker',r)
    payload,_=executor.context(lease);context=json.loads(payload['messages'][1]['content'])
    schema=context['report_schema']
    assert 'Fact' not in schema.get('$defs',{})
    assert schema['properties']['facts']['items']['$ref'].endswith('/FactV2')
    assert 'decimal strings stay strings' in payload['messages'][0]['content']
    assert 'ratio_point' in payload['messages'][0]['content']


def test_evidence_reference_metadata_uses_exact_registered_window_and_json_types(harness,executor):
    from analysis_agent.demo import CommerceAdapter,default_parameters
    adapter=CommerceAdapter();result=adapter.execute('compare@2',{},default_parameters()).model_dump(mode='json')
    catalog_hash=harness.objects.put(adapter.catalog())
    envelope={**result,'bindings':{'catalog':catalog_hash},'evidence_id':'example','tool_ref':'compare@2'}
    summary=executor.summary(envelope,1,1)
    assert summary['row_references']==[{'row':1,'entity':{'window':'baseline'},'time_range':{'start':'2025-01-01T00:00:00Z','end':'2025-01-08T00:00:00Z'}}]
    assert isinstance(summary['rows'][0]['refund_rate'],str)
    assert isinstance(summary['rows'][0]['orders'],int)
