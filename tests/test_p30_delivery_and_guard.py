import asyncio
import importlib.util
import json
from pathlib import Path
import sys
import pytest
from fastapi.testclient import TestClient
from analysis_agent.api import create_app
from analysis_agent.cli import main
from analysis_agent.contracts import Report
from analysis_agent.mcp_server import build_server
from test_runtime import harness,real_store,assert_error

@pytest.mark.parametrize('channel',['mcp_create','http_create','cli_create','mcp_skill','http_skill','cli_skill'])
def test_active_skill_body_every_delivery_channel_is_audited(harness,channel,tmp_path,monkeypatch,capsys):
    w=harness.workbench;r=w.create(harness.request,'same')['run_id'];before=len(harness.event_rows(r))
    server=build_server(w);client=TestClient(create_app(w,'x'*40));headers={'Authorization':'Bearer '+'x'*40}
    if channel=='mcp_create':output=str(asyncio.run(server.call_tool('create_run',{'request':harness.request.model_dump(),'idempotency_key':'same'})))
    elif channel=='http_create':output=client.post('/runs',json=harness.request.model_dump(),headers={**headers,'idempotency-key':'same'}).text
    elif channel=='cli_create':
        f=tmp_path/'request.json';f.write_text(harness.request.model_dump_json())
        monkeypatch.setattr(sys,'argv',['cli','create','--request',str(f),'--key','same']);main(lambda _:w);output=capsys.readouterr().out
    elif channel=='mcp_skill':output=str(asyncio.run(server.call_tool('get_run_skill',{'run_id':r})))
    elif channel=='http_skill':output=client.get(f'/runs/{r}/skill',headers=headers).text
    else:
        monkeypatch.setattr(sys,'argv',['cli','skill','--run',r]);main(lambda _:w);output=capsys.readouterr().out
    assert harness.registry.skills[harness.request.skill_ref].instructions in output
    events=harness.event_rows(r);assert len(events)==before+1
    assert events[-1]['kind']=='SKILL_DELIVERED'
    assert events[-1]['payload']['channel']==('create_replay' if channel.endswith('create') else 'get_run_skill')

@pytest.mark.parametrize('channel',['mcp','http','cli'])
def test_active_reopen_never_delivers_skill_body(harness,channel,monkeypatch,capsys):
    r=harness.create();w=harness.workbench;before=harness.event_rows(r)
    if channel=='mcp':output=str(asyncio.run(build_server(w).call_tool('reopen_run',{'run_id':r})))
    elif channel=='http':output=TestClient(create_app(w,'x'*40)).get(f'/runs/{r}',headers={'Authorization':'Bearer '+'x'*40}).text
    else:
        monkeypatch.setattr(sys,'argv',['cli','show','--run',r]);main(lambda _:w);output=capsys.readouterr().out
    assert harness.registry.skills[harness.request.skill_ref].instructions not in output
    assert harness.request.skill_ref in output
    assert harness.event_rows(r)==before


def test_cutoff_denial_counter_and_operator_escape(harness):
    h=harness;h.adapter.knowledge_cutoff=lambda p:p['last_day'];h.adapter.validate_task=lambda p:None
    h.workbench.project_policies={h.project:{'as_of_guard':True}}
    later=h.create()
    class Crash(BaseException):pass
    def stop(point,ctx):
        if point=='after_intent_commit':raise Crash()
    h.workbench._fault_hook=stop
    with pytest.raises(Crash):h.call(later)
    h.workbench._fault_hook=None
    attempt=h.workbench.reopen(later)['attempts'][0]['id']
    earlier=h.create(parameters={**h.request.parameters,'last_day':'2026-01-02'})
    for _ in range(4):assert_error('cross_run_read_refused',h.workbench.reopen,later)
    refused=[e for e in h.event_rows(earlier) if e['kind']=='CROSS_RUN_READ_REFUSED']
    assert len(refused)==1 and refused[0]['payload']['count']==1
    from sqlalchemy import select
    from analysis_agent.storage import access_refusals
    with h.store.engine.connect() as c:
        assert c.execute(select(access_refusals.c.count).where(access_refusals.c.run_id==earlier)).scalar_one()==4
    assert h.workbench.reconcile(later,older_than_seconds=0)['count']==1
    assert h.workbench.resolve(later,attempt,actor='operator',reason='Readonly fixture crash')['attempt_state']=='RESOLVED_FAILED'
    assert_error('cross_run_read_refused',h.call,later,key='blocked')
    assert_error('cross_run_read_refused',h.workbench.finalize,later,Report(result_status='insufficient_evidence',title='empty',facts=[]))
    assert h.row(later)['state']=='RUNNING'


def test_guard_rejects_uppercase_database(tmp_path):
    from database_guard import open_test_store,DatabaseGuardError,APPROVED_TEST_DATABASE
    from test_database_guard import FakeStore
    p=tmp_path/'synthetic.url';p.write_text('synthetic')
    with pytest.raises(DatabaseGuardError):open_test_store(p,store_factory=lambda _:FakeStore(APPROVED_TEST_DATABASE.upper()))


def scanner():
    file=Path(__file__).resolve().parents[1]/'scripts/package_public.py'
    spec=importlib.util.spec_from_file_location('public_scanner',file);module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module);return module

@pytest.mark.parametrize('marker',['/Users/' + 'sample/','private'+'_runtime','workbench'+'_private','aw_app'+'_private'])
def test_public_scan_refuses_forbidden_content_without_writing_package(tmp_path,marker):
    (tmp_path/'README.md').write_text(marker)
    with pytest.raises(ValueError):scanner().public_files(tmp_path)
    assert len(list(tmp_path.iterdir()))==1

def test_public_scan_omits_local_evidence_and_keeps_fixture_configuration(tmp_path):
    for name,value in [('docs/evidence/P2/private.txt','private'+'_runtime'),('eval/fixture_config.json','{}'),('README.md','public')]:
        p=tmp_path/name;p.parent.mkdir(parents=True,exist_ok=True);p.write_text(value)
    files={str(p.relative_to(tmp_path)) for p in scanner().public_files(tmp_path)}
    assert files=={'README.md','eval/fixture_config.json'}

def test_real_public_source_candidates_pass_scan_without_packaging():
    files=scanner().public_files(Path(__file__).resolve().parents[1])
    assert any(str(p).endswith('eval/fixture_config.json') for p in files)
