"""V02 subset: tested application paths, no claim of OS isolation from the owner."""
import asyncio
import ast
import importlib.util
import json
from pathlib import Path
import sys
import pytest
from fastapi.testclient import TestClient
from analysis_agent import cli
from analysis_agent.api import create_app
from analysis_agent.mcp_server import build_server
from analysis_agent.path_policy import EVALUATION_PRIVATE, runtime_path
from analysis_agent.storage import Objects
from analysis_agent.contracts import WorkbenchError
from test_runtime import harness, real_store, assert_error

ROOT=Path(__file__).resolve().parents[1]


def test_static_runtime_has_no_grader_or_answer_loader():
    # Public distribution checks never inspect an adjacent business package.
    files=list((ROOT/'src').rglob('*.py'))
    for path in files:
        source=path.read_text();tree=ast.parse(source)
        for node in ast.walk(tree):
            if isinstance(node,(ast.Import,ast.ImportFrom)):
                text=ast.unparse(node)
                assert all(name not in text for name in ('offline_grader','eval_private','solver','generator'))
        if path.name!='path_policy.py':assert 'eval_private' not in source
    from test_p30_delivery_and_guard import scanner
    assert all(EVALUATION_PRIVATE not in p.parents for p in scanner().public_files(ROOT))


@pytest.mark.parametrize('entry',['adapter','mcp','http','cli','objects'])
def test_runtime_entry_points_cannot_read_answer_path(harness,entry,monkeypatch,capsys):
    path=EVALUATION_PRIVATE/'variant_1_answers.json';attempted=[]
    original=Path.read_text
    def guarded(self,*args,**kwargs):
        resolved=self.resolve()
        if resolved==EVALUATION_PRIVATE or EVALUATION_PRIVATE in resolved.parents:
            attempted.append(str(resolved));raise AssertionError('Runtime tried to read answers')
        return original(self,*args,**kwargs)
    monkeypatch.setattr(Path,'read_text',guarded)
    r=harness.create();args={'entity':'alpha','day':'2026-01-02','file':str(path)}
    if entry=='adapter':assert_error('invalid_parameters',harness.call,r,args=args)
    elif entry=='mcp':
        from mcp.server.fastmcp.exceptions import ToolError
        with pytest.raises(ToolError,match='scope/schema'):
            asyncio.run(build_server(harness.workbench).call_tool('run_tool',{'run_id':r,'tool_ref':'read_counter@2','args':args,'action_key':'attack','explanation':'path probe'}))
    elif entry=='http':
        result=TestClient(create_app(harness.workbench,'x'*40)).post(f'/runs/{r}/actions',headers={'Authorization':'Bearer '+'x'*40},json={'tool_ref':'read_counter@2','args':args,'action_key':'attack','explanation':'path probe'})
        assert result.status_code==422
    elif entry=='cli':
        monkeypatch.setattr(sys,'argv',['workbench','create','--request',str(path),'--key','attack'])
        with pytest.raises(SystemExit):cli.main(factory=lambda args:pytest.fail('must reject before factory'))
        assert 'evaluation_path_forbidden' in capsys.readouterr().out
    else:assert_error('evaluation_path_forbidden',Objects,EVALUATION_PRIVATE)
    assert attempted==[] and harness.adapter.execute_calls==0


def test_symlink_alias_is_also_blocked(tmp_path):
    link=tmp_path/'alias';link.symlink_to(EVALUATION_PRIVATE,target_is_directory=True)
    assert_error('evaluation_path_forbidden',runtime_path,link/'answers.json')


def test_grader_only_accepts_completed_exports_and_detects_errors():
    spec=importlib.util.spec_from_file_location('offline_grader',ROOT/'eval/offline_grader.py');module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    rule=json.loads((ROOT/'eval/grader_spec.json').read_text())
    answer={'one':{'operation':'cell','inputs':[{'column':'x','entity':{'id':'synthetic'},'time':{'date':'2026-01-01'}}],'value':'10','unit':'count'}}
    fact={**answer['one'],'value':'10'}
    export={'run':{'state':'SUCCEEDED'},'report':{'validation':{'normalized_facts':[fact]}}}
    assert module.grade(export,answer,rule)[0]['status']=='PASSED'
    for value,error in [('999','wrong_value'),(True,'invalid_number'),('NaN','invalid_number')]:
        fact['value']=value;assert module.grade(export,answer,rule)[0]['error']==error
    fact['value']='10';fact['unit']='USD';assert module.grade(export,answer,rule)[0]['error']=='wrong_unit'
    export['run']['state']='RUNNING';assert module.grade(export,answer,rule)[0]['error']=='incomplete_export'
