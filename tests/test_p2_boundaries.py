"""Synthetic P2 storage classes, environment pinning, delivery and legacy access."""
import asyncio
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
import pytest
from sqlalchemy import update
from analysis_agent.contracts import Report, Registry, WorkbenchError, digest
from analysis_agent.demo import CommerceAdapter, demo_skill
from analysis_agent.mcp_server import build_server
from analysis_agent.runtime import Workbench
from analysis_agent.storage import Objects,runs
from analysis_agent import environment
from test_runtime import harness,real_store,assert_error,valid_report

@pytest.mark.parametrize('expected', ['public','private'])
def test_object_classes_missing_correct_and_wrong(tmp_path, expected):
    root=tmp_path/'objects'
    assert_error('store_class_missing',Objects.check_class,root,expected)
    assert not root.exists()
    Objects.initialize(root,expected)
    assert Objects.check_class(root,expected)==expected
    before=list(root.iterdir())
    assert_error('store_class_mismatch',Objects.check_class,root,'private' if expected=='public' else 'public')
    assert list(root.iterdir())==before
    assert not list(root.glob('*.json'))

@pytest.mark.parametrize('change',['package','adapter','entrypoint','runtime'])
def test_bound_environment_change_rejected_before_dispatch(harness,tmp_path,monkeypatch,change):
    entry=tmp_path/'launcher.py'; entry.write_text('# synthetic entry\n')
    harness.workbench.entrypoints=(str(entry),)
    r=harness.create(); manifest=harness.row(r)['manifest']
    assert manifest['schema_version']=='run_manifest@2'
    saved=harness.objects.get(manifest['environment_object'])
    assert digest(saved)==manifest['bindings']['environment']
    assert 'sqlalchemy' in {x.lower() for x in saved['loaded_distributions']}
    if change=='package':
        original=environment.metadata.version
        monkeypatch.setattr(environment.metadata,'version',lambda name: original(name)+'-changed')
    elif change=='entrypoint': entry.write_text('# changed entry\n')
    elif change=='runtime': monkeypatch.setattr(environment.platform,'python_version',lambda:'changed')
    else:
        original=Path.read_bytes
        module_path=Path(__import__('test_runtime').__file__)
        monkeypatch.setattr(Path,'read_bytes',lambda p: original(p)+b'\n# changed' if p==module_path else original(p))
    assert_error('version_changed',harness.call,r)
    assert harness.adapter.execute_calls==0
    assert harness.row(r)['tool_calls']==0


def test_manifest_one_reopens_without_adapter_or_environment(harness,monkeypatch):
    r=harness.create(); item=harness.call(r); harness.workbench.finalize(r,valid_report(item))
    # Synthetic legacy fixture, not a modification to a real old record.
    with harness.store.engine.begin() as c:
        manifest=dict(harness.row(r)['manifest']); manifest['schema_version']='run_manifest@1'
        manifest.pop('environment_object'); manifest.pop('knowledge_cutoff'); manifest.pop('store_class')
        # Preserve evidence bindings to retain checksum/association; v1 reader needs no new fields.
        c.execute(update(runs).where(runs.c.id==r).values(manifest=manifest))
    monkeypatch.setattr(environment,'capture',lambda *a,**k:pytest.fail('legacy reopen captured environment'))
    assert harness.workbench.reopen(r)['report']['validation']['status']=='valid'


def test_skill_delivery_hash_attachments_and_terminal_read(harness):
    skill=harness.registry.skills[harness.request.skill_ref]
    skill=skill.model_copy(update={'attachments':{'references/test.md':'Synthetic attachment'}})
    harness.registry.skills[skill.ref]=skill
    created=harness.workbench.create(harness.request,'delivery'); r=created['run_id']
    payload=created['skill']; assert payload['instructions']==skill.instructions
    assert payload['attachments']==[{'filename':'references/test.md','sha256':hashlib.sha256(b'Synthetic attachment').hexdigest()}]
    assert payload['skill_digest']==harness.row(r)['manifest']['bindings']['skill']
    assert harness.workbench.get_run_skill(r,'references/test.md')['attachment']['content']=='Synthetic attachment'
    events=harness.event_rows(r)
    assert [e['payload']['channel'] for e in events if e['kind']=='SKILL_DELIVERED']==['create','get_run_skill']
    assert all(e['payload']['skill_digest']==payload['skill_digest'] for e in events if e['kind']=='SKILL_DELIVERED')
    assert_error('attachment_not_found',harness.workbench.get_run_skill,r,'../secret')
    harness.workbench.finalize(r,Report(result_status='insufficient_evidence',title='No evidence',facts=[]))
    before=harness.event_rows(r); objects=set(harness.objects.root.iterdir())
    harness.workbench.get_run_skill(r,'references/test.md')
    assert harness.event_rows(r)==before and set(harness.objects.root.iterdir())==objects


def test_mcp_delivers_skill_and_does_not_offer_resolutions(harness):
    server=build_server(harness.workbench)
    tools=asyncio.run(server.list_tools()); names={t.name for t in tools}
    assert 'get_run_skill' in names and 'resolutions' not in names
    r=harness.create()
    result=asyncio.run(server.call_tool('get_run_skill',{'run_id':r}))
    assert harness.row(r)['manifest']['bindings']['skill'] in str(result)
    assert harness.event_rows(r)[-1]['payload']['channel']=='get_run_skill'


def test_blinded_catalog_capabilities_and_mcp(harness):
    adapters=[CommerceAdapter(),CommerceAdapter('normal')]
    w=Workbench(harness.store,harness.objects,Registry(adapters,[demo_skill()]),harness.actor,{harness.project:[a.ref for a in adapters]})
    server=build_server(w)
    value={'catalogs':[a.catalog() for a in adapters],'capabilities':[a.capabilities() for a in adapters],
           'mcp':str(asyncio.run(server.call_tool('capabilities',{'project_id':harness.project})))}
    serialized=json.dumps(value).lower()
    assert all(label not in serialized for label in ['normal','anomaly','variant'])
    assert len({a.ref for a in adapters})==2


def test_legacy_source_allowed_only_for_read(harness):
    r=harness.create()
    with harness.store.engine.begin() as c:
        manifest=harness.row(r)['manifest']; manifest['request']['source_ref']='synthetic_commerce@1'
        manifest['schema_version']='run_manifest@1'
        c.execute(update(runs).where(runs.c.id==r).values(manifest=manifest))
    harness.workbench.legacy_sources={harness.project:['synthetic_commerce@1']}
    assert harness.workbench.reopen(r)['run']['id']==r
    assert_error('forbidden',harness.create,source_ref='synthetic_commerce@1')
    assert_error('forbidden',harness.call,r)


def test_readonly_workbench_rejects_mutations(harness):
    r=harness.create(); w=harness.workbench; w.readonly=True
    before=harness.event_rows(r); objects=set(harness.objects.root.iterdir())
    assert_error('legacy_readonly',harness.create)
    assert_error('legacy_readonly',harness.call,r)
    assert_error('legacy_readonly',w.finalize,r,Report(result_status='insufficient_evidence',title='No source',facts=[]))
    assert_error('legacy_readonly',w.reconcile,r,older_than_seconds=0)
    assert_error('legacy_readonly',w.resolve,r,'none',actor='x',reason='x')
    assert_error("skill_delivery_requires_writable", w.get_run_skill, r); w.reopen(r)
    assert harness.event_rows(r)==before and set(harness.objects.root.iterdir())==objects


def test_reader_identity_mismatch_is_failed_without_accepting_evidence(harness,monkeypatch):
    def wrong_identity(*a,**k):raise WorkbenchError('reader_identity_mismatch','Synthetic wrong reader')
    monkeypatch.setattr(harness.adapter,'execute',wrong_identity)
    r=harness.create();assert_error('reader_identity_mismatch',harness.call,r)
    record=harness.workbench.reopen(r)
    assert record['attempts'][0]['state']=='FAILED'
    assert record['attempts'][0]['error_code']=='reader_identity_mismatch'
    assert record['steps'][0]['evidence'] is None
