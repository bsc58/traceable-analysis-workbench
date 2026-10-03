"""P5 source mutation, interrupted exports and offline replay: synthetic only."""
import json, socket
import pytest
import pyarrow as pa
from sqlalchemy import delete, select
from analysis_agent.snapshots import Snapshots
from analysis_agent.frozen_demo import freeze_commerce, FrozenCommerceAdapter
from analysis_agent.demo import CommerceAdapter, demo_skill, default_parameters
from analysis_agent.contracts import Registry, RunRequest, ResultSpec, RequiredFact, Report, WorkbenchError
from analysis_agent.storage import dataset_versions, snapshot_sets
from test_runtime import real_store, harness, assert_error


@pytest.fixture
def manager(real_store,tmp_path,request):
    value=Snapshots(real_store,tmp_path/'snapshots',getattr(request,'param','public'))
    with real_store.engine.connect() as c:prior=set(c.execute(select(dataset_versions.c.id)).scalars())
    yield value
    with real_store.engine.begin() as c:
        ids=set(c.execute(select(dataset_versions.c.id)).scalars())-prior
        c.execute(delete(snapshot_sets).where(snapshot_sets.c.dataset_id.in_(ids)))
        c.execute(delete(dataset_versions).where(dataset_versions.c.id.in_(ids)))


def test_d01_frozen_queries_ignore_mutated_source(manager):
    live=CommerceAdapter('anomaly');snapshot=freeze_commerce(manager,live);frozen=FrozenCommerceAdapter(snapshot)
    before=frozen.execute('compare@2',{},default_parameters());live._refunds=()
    after=frozen.execute('compare@2',{},default_parameters())
    assert before.rows==after.rows and after.rows!=live.execute('compare@2',{},default_parameters()).rows
    assert after.provenance['source_queries_executed']==2
    assert after.provenance['data_version']['queryable_snapshot'] is True
    assert after.provenance['data_version']['as_known_at_supported'] is False
    assert frozen.tools==live.tools


@pytest.mark.parametrize('boundary',['after_batch','before_publish'])
def test_d05_interruption_leaves_only_staging(manager,boundary):
    def kill(point):
        if point==boundary:raise RuntimeError('interrupted')
    with pytest.raises(RuntimeError):freeze_commerce(manager,CommerceAdapter(),kill)
    with manager.store.engine.connect() as c:
        row=c.execute(select(snapshot_sets).order_by(snapshot_sets.c.created_at.desc())).mappings().first()
    assert row['state']=='STAGING'
    assert_error('snapshot_unavailable',manager.open,row['id'])


def test_d07_cursor_bound_to_version_and_query(manager):
    one=freeze_commerce(manager,CommerceAdapter());two=freeze_commerce(manager,CommerceAdapter())
    token=one.cursor(200,{'day':'2025-01-08'});assert one.offset(token,{'day':'2025-01-08'})==200
    assert_error('cursor_mismatch',two.offset,token,{'day':'2025-01-08'})
    assert_error('cursor_mismatch',one.offset,token,{'day':'2025-01-09'})


@pytest.mark.parametrize('state',['RETIRED','EXPIRED'])
def test_lifecycle_disallows_new_queries(manager,state):
    snapshot=freeze_commerce(manager,CommerceAdapter());manager.retire(snapshot.key,state)
    assert_error('snapshot_unavailable',snapshot.connection)
    assert_error('snapshot_state',manager.retire,snapshot.key)


def test_checksum_tamper_fails_before_duckdb_query(manager):
    snapshot=freeze_commerce(manager,CommerceAdapter());info=snapshot.manifest['files']['orders']
    path=manager.root/snapshot.manifest['dataset_id']/info['file'];path.chmod(0o600);path.write_bytes(b'corrupt')
    assert_error('snapshot_corrupt',snapshot.connection)


def test_duckdb_external_files_network_and_config_locked(manager,tmp_path):
    snapshot=freeze_commerce(manager,CommerceAdapter());secret=tmp_path/'secret.txt';secret.write_text('no')
    with snapshot.connection() as con:
        assert con.execute("SELECT current_setting('enable_external_access')").fetchone()==(False,)
        assert con.execute('SELECT count(*) FROM orders').fetchone()[0]==84
        for sql in [f"SELECT * FROM read_text('{secret}')", "SELECT * FROM read_json('https://example.com/data')",'SET enable_external_access=true']:
            with pytest.raises(Exception):con.execute(sql)


def test_v07_offline_replay_and_v08_actual_new_query(harness,manager,monkeypatch):
    live=CommerceAdapter();frozen=FrozenCommerceAdapter(freeze_commerce(manager,live));skill=demo_skill()
    from analysis_agent.runtime import Workbench
    w=Workbench(harness.store,harness.objects,Registry([frozen,live],[skill]),harness.actor,{harness.project:[frozen.ref,live.ref]})
    request=RunRequest(project_id=harness.project,source_ref=frozen.ref,skill_ref=skill.ref,question='Synthetic',parameters=default_parameters(),consistency='frozen',result_spec=ResultSpec(requirements=[RequiredFact(column='orders')]))
    first=w.create(request,'first')['run_id'];e=w.call(first,'compare@2',{},'q','read frozen')
    w.finalize(first,Report(result_status='insufficient_evidence',title='Recorded',facts=[]))
    live._refunds=()
    with monkeypatch.context() as m:
        m.setattr(socket,'create_connection',lambda *a,**k:pytest.fail('unexpected network'))
        m.setattr(frozen,'execute',lambda *a,**k:pytest.fail('replay queried snapshot'))
        assert w.replay(first)['source_queries_executed']==0
    second=w.rerun_frozen(first,'second')['run_id'];assert second!=first
    e2=w.call(second,'compare@2',{},'q2','new frozen query');assert e2['rows']==e['rows']
    # Latest needs a newly registered current catalog ref after mutation.
    current=live.ref;w.registry=Registry([frozen,live],[skill]);w.projects[harness.project].append(current)
    latest=w.run_latest(first,current,'latest')['run_id']
    assert w.call(latest,'compare@2',{},'q3','latest')['rows']!=e['rows']
