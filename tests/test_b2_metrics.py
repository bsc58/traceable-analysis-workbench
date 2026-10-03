import importlib.util
from pathlib import Path
import pytest
P=Path(__file__).resolve().parents[1]/'eval/b2/metrics.py'
spec=importlib.util.spec_from_file_location('b2_metrics',P);metrics=importlib.util.module_from_spec(spec);spec.loader.exec_module(metrics)

def trial(i,**changes):
    return dict(trial_id=str(i),task_id='t',family='f',scenario='commerce',condition='A1',provider='xai',split='holdout',status='PASSED',correct=True,answerable=True,stopped=False,necessary_total=2,necessary_correct=2,necessary_reported=2,input_tokens=10,output_tokens=2,cost_micros=20,currency='USD',tool_calls=1,wall_seconds=2,failures=[],injection_proposed=0,injection_blocked=0,injection_succeeded=0,**changes)

def test_all_planned_and_failed_trials_stay_in_denominator():
    a=trial(1);b={**trial(2),'correct':False,'status':'NOT RUN','necessary_correct':0,'necessary_reported':0}
    result=metrics.aggregate([a,b]);assert result['completion']=={'numerator':1,'denominator':2,'value':.5}
    assert result['necessary_coverage']['value']==.5 and result['necessary_accuracy']['value']==1

def test_zero_denominators_and_separate_currencies():
    a={**trial(1),'answerable':False,'stopped':True,'necessary_total':0,'necessary_correct':0,'necessary_reported':0}
    b={**trial(2),'currency':'CNY'}
    result=metrics.aggregate([a,b]);assert result['unanswerable_correct_stop']['value']==1
    assert result['cost_micros_by_currency']=={'USD':20,'CNY':20}
    assert metrics.aggregate([a])['necessary_accuracy']['value'] is None

def test_injection_excluded_and_duplicate_rejected():
    a={**trial(2),'split':'injection','injection_proposed':2,'injection_blocked':1,'injection_succeeded':1}
    result=metrics.summarize([trial(1),a]);assert all(r['trials']==1 for r in result['groups'])
    assert result['injection']['proposed']==2 and result['injection']['succeeded']==1
    with pytest.raises(ValueError):metrics.summarize([trial(1),trial(1)])

def test_cluster_pairing_is_by_task_and_keeps_failed_replicates():
    rows=[]
    for t in range(3):
        rows.append({**trial('b'+str(t)),'task_id':str(t),'family':str(t),'condition':'B0','provider':'b0','correct':False})
        for rep in range(3):rows.append({**trial(str(t)+str(rep)),'task_id':str(t),'family':str(t),'correct':rep<2})
    a=metrics.paired(rows,'B0-b0');assert a['paired_tasks']==3 and a['families']==3
    assert a['difference']==pytest.approx(2/3) and a['ci95']==pytest.approx([2/3,2/3])
    assert metrics.paired(list(reversed(rows)),'B0-b0')==a
    assert metrics.paired(rows,'A0-xai')['status']=='UNSUPPORTED'
