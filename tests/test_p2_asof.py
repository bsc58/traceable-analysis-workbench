"""The project guard constrains workbench channels, not external Agent information."""
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import update
from analysis_agent.api import create_app
from analysis_agent.contracts import Report
from analysis_agent.storage import runs
from test_runtime import harness,real_store,assert_error,valid_report

def setup(h):
    h.adapter.knowledge_cutoff=lambda p:p['last_day']
    h.adapter.validate_task=lambda p:None
    h.workbench.project_policies={h.project:{'as_of_guard':True}}
    late=h.create(); item=h.call(late); h.workbench.finalize(late,valid_report(item))
    early=h.create(parameters={**h.request.parameters,'last_day':'2026-01-02'})
    return early,late,item

@pytest.mark.parametrize('channel',['reopen','evidence','skill','resolutions','view','events'])
def test_later_run_read_refused_and_audited_on_earliest(harness,channel):
    early,late,item=setup(harness)
    late_events=harness.event_rows(late); before=len(harness.event_rows(early))
    w=harness.workbench
    if channel=='reopen': assert_error('cross_run_read_refused',w.reopen,late)
    elif channel=='evidence': assert_error('cross_run_read_refused',w.read_evidence,late,item['evidence_id'])
    elif channel=='skill': assert_error('cross_run_read_refused',w.get_run_skill,late)
    else:
        client=TestClient(create_app(w,'x'*40))
        response=client.get(f'/runs/{late}/{channel}',headers={'Authorization':'Bearer '+'x'*40})
        assert response.status_code==403 and response.json()['error']['code']=='cross_run_read_refused'
    assert harness.event_rows(late)==late_events
    audit=harness.event_rows(early)
    assert len(audit)==before+1 and audit[-1]['kind']=='CROSS_RUN_READ_REFUSED'
    assert audit[-1]['payload']['target_run_id']==late
    assert [x['id'] for x in w.history(harness.project)]==[early]


def test_unknown_legacy_cutoff_is_hidden_until_every_run_ends(harness):
    early,late,item=setup(harness)
    with harness.store.engine.begin() as c:
        manifest=harness.row(late)['manifest']; manifest.pop('knowledge_cutoff'); manifest['schema_version']='run_manifest@1'
        c.execute(update(runs).where(runs.c.id==late).values(manifest=manifest))
    assert_error('cross_run_read_refused',harness.workbench.reopen,late)
    harness.workbench.finalize(early,Report(result_status='insufficient_evidence',title='No accepted evidence',facts=[]))
    assert harness.workbench.reopen(late)['run']['id']==late
    assert {x['id'] for x in harness.workbench.history(harness.project)}=={early,late}


def test_two_active_cutoffs_only_earliest_can_read_until_it_ends(harness):
    h=harness; h.adapter.knowledge_cutoff=lambda p:p['last_day']; h.adapter.validate_task=lambda p:None
    h.workbench.project_policies={h.project:{'as_of_guard':True}}
    first=h.create(parameters={**h.request.parameters,'last_day':'2026-01-02'})
    later=h.create()
    assert_error('cross_run_read_refused',h.workbench.reopen,later)
    h.workbench.finalize(first,Report(result_status='insufficient_evidence',title='No source',facts=[]))
    assert h.workbench.reopen(later)['run']['id']==later
    h.workbench.finalize(later,Report(result_status='insufficient_evidence',title='No source',facts=[]))
    assert {x['id'] for x in h.workbench.history(h.project)}=={first,later}
