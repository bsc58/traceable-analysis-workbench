"""Fixed expectations registered in eval/p3_validator_cases.json before execution."""
import json
from pathlib import Path
import pytest
from pydantic import ValidationError
from sqlalchemy import select
from analysis_agent.contracts import Report, RunRequest, FactV2, ResultSpec, RequiredFact
from analysis_agent.validation import validate_report
from analysis_agent.presentation import render_record
from analysis_agent.runtime import Workbench
from analysis_agent.storage import events
from test_runtime import harness, real_store, valid_report, assert_error

REGISTERED=json.loads((Path(__file__).resolve().parents[1]/'eval/p3_validator_cases.json').read_text())
CORPUS=REGISTERED['cases']

def save_outcome(case,result):
    folder=Path(__file__).resolve().parents[1]/'docs/evidence/BATCH1/corpus'
    folder.mkdir(parents=True,exist_ok=True)
    (folder/(case['id']+'.json')).write_text(json.dumps({'case':case['id'],'expected':case['expected'],'actual':result},indent=2)+'\n')

def evaluate(case):
    return validate_report(Report.model_validate(case['report']),lambda key:case['evidence'][key],
                           result_spec=case['result_spec'],catalog=case['catalog'])

@pytest.mark.parametrize('case',CORPUS,ids=lambda c:c['id'])
def test_fixed_adversarial_corpus(case):
    result=evaluate(case)
    save_outcome(case,result)
    assert result['status']==case['expected']['status']
    assert set(x['code'] for x in result['errors']+result['warnings'])==set(case['expected']['codes'])


def test_result_contract_rejects_legacy_field_for_new_run(harness):
    values=harness.request.model_dump()
    with pytest.raises(ValidationError):RunRequest(**values,required_fact_columns=[])
    legacy=harness.request.model_copy(update={'result_spec':None,'required_fact_columns':['requests']})
    assert_error('legacy_result_contract',harness.workbench.create,legacy,'legacy')


def test_free_label_is_not_a_fact_v2_field():
    fact=CORPUS[4]['report']['facts'][0]
    with pytest.raises(ValidationError):FactV2(**fact,label='Invented conclusion')


def test_warning_publication_event_records_actual_status(harness):
    run=harness.create();report=valid_report(harness.call(run)).model_copy(update={'hypotheses':['Unverified 999']})
    artifact=harness.workbench.finalize(run,report)
    assert artifact['validation']['status']=='warning'
    assert harness.event_rows(run)[-1]['payload']['validation_status']=='warning'
    reopened=harness.workbench.reopen(run)
    page=render_record(reopened)
    assert page.index('校验结果')<page.index('标题（未校验）')
    for field in ('校验范围','result_status','数据模式'):assert page.index(field)<page.index('<h1>')
    assert '文字区（未校验）' in page and 'Unverified 999' in page
    assert all(word not in page for word in ('分析正确','已验证'))
    assert 'Unverified 999' not in page.split('id="structured-facts"')[1].split('</section>')[0]


@pytest.mark.parametrize('case',REGISTERED['scope_cases'],ids=lambda c:c['id'])
def test_reference_scope_rejected_before_coverage(harness,case):
    scope=case['id']
    first=harness.create();item=harness.call(first);target=harness.create();harness.call(target)
    if scope=='other_project':
        from sqlalchemy import update
        from analysis_agent.storage import runs
        with harness.store.engine.begin() as c:c.execute(update(runs).where(runs.c.id==first).values(project_id=harness.project+'-foreign'))
    elif scope=='unaccepted_attempt':
        # Existing rejected result is a CAS object only, never an accepted evidence identifier.
        item={**item,'evidence_id':harness.objects.put({'result':'unaccepted synthetic attempt'})}
    result=harness.workbench.finalize(target,valid_report(item))
    save_outcome(case,result['validation'])
    assert result['validation']['status']==case['expected']['status']
    assert set(e['code'] for e in result['validation']['errors'])==set(case['expected']['codes'])
    if scope=='other_project':
        with harness.store.engine.begin() as c:c.execute(update(runs).where(runs.c.id==first).values(project_id=harness.project))


def test_legacy_fact_label_becomes_unverified_note(harness):
    run=harness.create();report=valid_report(harness.call(run));report=report.model_copy(update={'facts':[report.facts[0].model_copy(update={'label':'Count 999'})]})
    result=harness.workbench.finalize(run,report)['validation']
    assert result['status']=='warning'
    assert result['normalized_facts'][0]['note']=='Count 999'
    assert '999' not in result['generated_labels'][0]['label']
