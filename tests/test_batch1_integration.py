"""Actual host integration without editing parallel-owned source or tests."""
import sys
import pytest
from fastapi.testclient import TestClient
from analysis_agent.api import create_app
from analysis_agent import cli
from analysis_agent.scenarios import build_registry
from test_runtime import harness, real_store
from test_p4_executor import executor


def test_parallel_local_evidence_is_not_a_public_candidate(tmp_path):
    from test_p30_delivery_and_guard import scanner
    for name in ['docs/ui/evidence/local.txt', 'docs/scenarios/evidence/local.txt']:
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('/Users/' + 'fixture/local/')
    (tmp_path / 'README.md').write_text('public')
    assert scanner().public_files(tmp_path) == [tmp_path / 'README.md']
    (tmp_path / 'alias').symlink_to(tmp_path / 'README.md')
    with pytest.raises(ValueError, match='Symlinks'):
        scanner().public_files(tmp_path)


def test_host_mounts_authenticated_ui(harness):
    client=TestClient(create_app(harness.workbench,'x'*40))
    assert client.get('/ui/projects').status_code==401
    result=client.get('/ui/projects',headers={'Authorization':'Bearer '+'x'*40})
    assert result.json()['projects']==[harness.project]


def test_ui_creation_queues_internal_run_and_shows_model_budgets(harness,executor):
    client=TestClient(create_app(harness.workbench,'x'*40,executor=executor,execution_config=executor.config));headers={'Authorization':'Bearer '+'x'*40}
    config=client.get(f'/ui/projects/{harness.project}/configuration',headers=headers).json()
    assert config['budgets']['model_execution']['provider']=='fixture'
    result=client.post('/runs',headers={**headers,'Idempotency-Key':'ui-internal'},json=harness.request.model_dump(mode='json'))
    run=result.json()['run_id'];assert harness.row(run)['manifest']['execution']['driver']=='internal_runner'
    executor.run('worker',run)
    detail=client.get('/ui/runs/'+run+'/detail',headers=headers).json()
    assert detail['run']['state']=='SUCCEEDED' and detail['checked_facts']


@pytest.mark.parametrize('name',['service_ops','data_quality'])
def test_cli_receives_registered_scenario(name,monkeypatch,capsys):
    seen=[]
    class W:
        def history(self,project):seen.append(project);return []
    def factory(args):
        assert args.scenario==name;assert build_registry(args.scenario).adapters;return W()
    monkeypatch.setattr(sys,'argv',['workbench','--scenario',name,'history','--project',name])
    cli.main(factory=factory);assert seen==[name] and capsys.readouterr().out.strip()=='[]'
