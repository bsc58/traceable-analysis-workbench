"""Five model/publish boundaries, ten real SIGKILL experiments at each."""
from pathlib import Path
import json,os,signal,subprocess,sys,uuid
import pytest
from sqlalchemy import delete,select,update
from analysis_agent.execution import Executor,ExecutionConfig
from analysis_agent.providers import FixtureProvider
from analysis_agent.storage import budget_accounts,model_invocations,run_execution,runs
from kill_scenario import build_scenario,cleanup_project
from test_p4_executor import responder,batch_config

POINTS=['model_after_intent_commit','model_after_response_before_cas','model_after_cas_before_accept','model_after_accept_commit','during_report_publish']

@pytest.mark.parametrize('point',POINTS)
@pytest.mark.parametrize('repeat',range(10))
def test_model_sigkill_recovery(point,repeat,tmp_path):
    filename=os.environ.get('WORKBENCH_TEST_DB_URL_FILE')
    if not filename:pytest.skip('Approved synthetic PostgreSQL required')
    unique=uuid.uuid4().hex
    config={'url_file':str(Path(filename).resolve()),'objects':str(tmp_path/'objects'),'project':'p4-kill-'+unique,
        'actor':'actor-'+unique,'point':point,'marker':str(tmp_path/'marker.json')}
    scenario=build_scenario(config);e=Executor(scenario.workbench,FixtureProvider(responder));account='kill-'+unique
    e.register_batch(account,batch_config())
    ec=ExecutionConfig(provider='fixture',model='fixture@1',batch_account=account)
    r=e.submit(scenario.request,'create',ec)['run_id'];config['run_id']=r
    cfg=tmp_path/'config.json';cfg.write_text(json.dumps(config))
    try:
        child=subprocess.run([sys.executable,str(Path(__file__).with_name('p4_kill_worker.py')),str(cfg)],capture_output=True,text=True,timeout=20)
        assert child.returncode==-signal.SIGKILL,child.stderr
        marker=json.loads(Path(config['marker']).read_text());assert marker['fsynced'] and marker['point']==point
        before=scenario.workbench.reopen(r)
        with scenario.store.engine.begin() as c:c.execute(update(runs).where(runs.c.id==r).values(lease_expires_at=0))
        lease=e.claim('recovery',r);assert lease.epoch==2
        recovered=e.recover(lease)
        expected_saved=point not in {'model_after_intent_commit','model_after_response_before_cas'}
        assert recovered==expected_saved
        if not recovered:
            after=scenario.workbench.reopen(r)
            unknown=[x for x in after['model_invocations'] if x['kind']=='UNKNOWN'];assert len(unknown)==1
            with scenario.store.engine.connect() as c:
                assert c.execute(select(budget_accounts.c.held).where(budget_accounts.c.id==account)).scalar_one()['output_tokens']==ec.max_output_tokens
            e.resolve_unknown(r,unknown[0]['invocation_id'],'Synthetic kill: abandon response, keep reservation')
            e.resume(r)
        else:
            with scenario.store.engine.begin() as c:c.execute(update(runs).where(runs.c.id==r).values(lease_expires_at=0))
        result=e.run('finisher',r)
        assert result['run']['state']=='SUCCEEDED'
        assert sum(x['kind']=='REPORT_PUBLISHED' for x in result['events'])==1
        target=Path(os.environ.get('WORKBENCH_P4_KILL_EVIDENCE_DIR',str(Path(__file__).resolve().parents[1]/'docs/evidence/BATCH1/p4-kill')))
        target.mkdir(parents=True,exist_ok=True)
        (target/f'{point}-{repeat:02}.json').write_text(json.dumps({'point':point,'repeat':repeat,'returncode':child.returncode,'marker':marker,
            'before':before,'after':result,'expected_saved_response':expected_saved,'status':'PASSED'},indent=2)+'\n')
    finally:
        with scenario.store.engine.begin() as c:
            c.execute(delete(model_invocations).where(model_invocations.c.run_id==r))
            c.execute(delete(run_execution).where(run_execution.c.run_id==r))
            c.execute(delete(budget_accounts).where(budget_accounts.c.id==account))
        cleanup_project(scenario,config['project'])
        e.close();scenario.store.engine.dispose()
