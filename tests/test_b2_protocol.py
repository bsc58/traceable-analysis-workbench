"""Development-only benchmark checks; no model held-out results are read."""
import ast,importlib.util,json,os,sys,uuid
from pathlib import Path
import pytest
from analysis_agent.execution import Executor
from analysis_agent.providers import FixtureProvider
from analysis_agent.runtime import Workbench
from analysis_agent.storage import Objects
from test_runtime import real_store
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT/'eval/b2'))
from generate import generate_cases
from harness import assemble,fixed_baseline,PRIMARY_TOOLS
from grader import grade,equal

def load_reference():
    p=Path(os.environ.get('B2_REFERENCE_DIR',ROOT.parent.parent/'work/eval_private/b2'))/'solver.py'
    if not p.exists():pytest.skip('Owner-controlled independent reference unavailable in public install')
    assert not p.resolve().is_relative_to(ROOT)
    spec=importlib.util.spec_from_file_location('independent_b2',p);m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m);return m

@pytest.mark.parametrize('case',generate_cases('development'),ids=lambda c:c['id'])
def test_development_fixed_baseline_against_independent_reference(real_store,tmp_path,case):
    reference=load_reference();adapter,registry,request=assemble(case,'B0','dev-'+uuid.uuid4().hex)
    expected_tables=reference.calculate_tables(case)
    for tool,rows in expected_tables.items():
        args={'dimension':'channel'} if tool=='breakdown@2' else {}
        actual=adapter.execute(tool,args,case['parameters']).rows
        assert len(actual)==len(rows)
        assert all(set(a)==set(b) and all(type(a[k]) is type(b[k]) and equal(a[k],b[k]) for k in a) for a,b in zip(actual,rows))
    w=Workbench(real_store,Objects(tmp_path/'objects'),registry,'fixture',{request.project_id:[adapter.ref]})
    result=fixed_baseline(w,request,case,'fixed')
    assert grade(result,reference.solve(case))['correct']


def test_input_generator_and_reference_imports_remain_independent():
    reference=load_reference()
    for file in [ROOT/'eval/b2/generate.py',Path(reference.__file__)]:
        tree=ast.parse(file.read_text())
        names=[ast.unparse(n) for n in ast.walk(tree) if isinstance(n,(ast.Import,ast.ImportFrom))]
        assert not any('analysis_agent' in n or 'harness' in n for n in names)
    cases=generate_cases('development');assert len(cases)==9 and len({c['family'] for c in cases})==3


def test_model_context_cannot_observe_selection_or_reference_material(real_store,tmp_path,monkeypatch):
    from analysis_agent.execution import BatchConfig,ExecutionConfig
    project='isolation-'+uuid.uuid4().hex
    case=generate_cases('development')[0];adapter,registry,request=assemble(case,'A1',project)
    w=Workbench(real_store,Objects(tmp_path/'objects'),registry,'fixture',{project:[adapter.ref]})
    provider=FixtureProvider([]);e=Executor(w,provider)
    try:
        e.register_batch('b2-context-test',BatchConfig(provider='fixture',model='fixture',returned_models=('fixture',),currency='fixture',input_per_million='0',output_per_million='0',pricing_source='fixture',pricing_date='2026-10-02',cost_micros=1))
        run=e.submit(request,'context',ExecutionConfig(provider='fixture',model='fixture',batch_account='b2-context-test'))['run_id'];lease=e.claim('worker',run)
        original=Path.read_text
        def guarded(p,*a,**k):
            assert 'eval_private' not in p.parts
            return original(p,*a,**k)
        monkeypatch.setattr(Path,'read_text',guarded)
        payload,_=e.context(lease);text=json.dumps(payload)
        assert case['id'] not in text and case['family'] not in text
        assert all(term not in text for term in ['necessary_facts','expected_values','solver.py','eval_private','holdout','variant'])
        assert set(json.loads(payload['messages'][1]['content'])) >= {'catalog','tools','task','skill'}
    finally:e.close()
