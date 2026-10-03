"""Schema v3 and immutable source revalidation using only guarded synthetic PG."""
from types import SimpleNamespace
import json
import sys
import pytest
from sqlalchemy import delete, select
from analysis_agent.contracts import WorkbenchError, digest
from analysis_agent.migrations import legacy_digest
from analysis_agent.storage import revalidations
from analysis_agent.revalidation import revalidate
from analysis_agent import cli
from database_guard import migrate_test_store, rollback_test_store, FakeMigrationProbe
from test_runtime import harness, real_store, valid_report, assert_error


def test_v2_v3_v2_rehearsal_preserves_all_prior_tables(real_store):
    with real_store.engine.connect() as c:
        assert c.execute(select(revalidations.c.id)).first() is None
        before=legacy_digest(c)
    assert rollback_test_store(real_store,from_version=7,to_version=6)['metadata_schema']==6
    assert rollback_test_store(real_store,from_version=6,to_version=5)['metadata_schema']==5
    assert rollback_test_store(real_store,from_version=5,to_version=4)['metadata_schema']==4
    assert rollback_test_store(real_store,from_version=4,to_version=3)['metadata_schema']==3
    assert rollback_test_store(real_store,from_version=3,to_version=2)['metadata_schema']==2
    assert real_store.check_version(legacy_readonly=True)==2
    assert_error('migration_required',real_store.check_version)
    assert migrate_test_store(real_store,from_version=2,to_version=3)['metadata_schema']==3
    assert rollback_test_store(real_store,from_version=3,to_version=2)['metadata_schema']==2
    assert migrate_test_store(real_store,from_version=2,to_version=3)['metadata_schema']==3
    with real_store.engine.connect() as c:assert legacy_digest(c)==before
    assert migrate_test_store(real_store,from_version=3,to_version=4)["metadata_schema"]==4
    assert migrate_test_store(real_store,from_version=4,to_version=5)["metadata_schema"]==5
    assert migrate_test_store(real_store,from_version=5,to_version=6)["metadata_schema"]==6
    assert migrate_test_store(real_store,from_version=6,to_version=7)["metadata_schema"]==7


def test_v3_revalidation_does_not_touch_original_report_run_or_events(harness):
    run=harness.create();harness.workbench.finalize(run,valid_report(harness.call(run)))
    before=harness.workbench.reopen(run);before_hash=digest(before)
    harness.workbench.readonly=True
    try:
        result=revalidate(harness.workbench,harness.store,harness.objects,run,'cell_validator@2')
        assert result['original_report_hash']==before['run']['report_hash']
        assert result['validation']['status']=='valid'
        assert harness.objects.get(result['result_hash'])['source_records_changed'] is False
        assert digest(harness.workbench.reopen(run))==before_hash
        with harness.store.engine.connect() as c:
            assert c.execute(select(revalidations).where(revalidations.c.id==result['id'])).mappings().one()['run_id']==run
        rollback_test_store(harness.store,from_version=7,to_version=6)
        rollback_test_store(harness.store,from_version=6,to_version=5)
        rollback_test_store(harness.store,from_version=5,to_version=4)
        rollback_test_store(harness.store,from_version=4,to_version=3)
        assert_error('rollback_unsafe',rollback_test_store,harness.store,from_version=3,to_version=2)
        migrate_test_store(harness.store,from_version=3,to_version=4)
        migrate_test_store(harness.store,from_version=4,to_version=5)
        migrate_test_store(harness.store,from_version=5,to_version=6)
        migrate_test_store(harness.store,from_version=6,to_version=7)
    finally:
        with harness.store.engine.begin() as c:c.execute(delete(revalidations).where(revalidations.c.run_id==run))


@pytest.mark.parametrize('problem',['active','unknown_validator','privacy','foreign_actor'])
def test_revalidation_admission(harness,problem):
    run=harness.create()
    if problem!='active':harness.workbench.finalize(run,valid_report(harness.call(run)))
    validator='unknown@1' if problem=='unknown_validator' else 'cell_validator@2'
    objects=SimpleNamespace(store_class='different') if problem=='privacy' else harness.objects
    if problem=='foreign_actor':harness.workbench.actor='stranger'
    before=harness.row(run);events=harness.event_rows(run)
    code={'active':'report_not_final','unknown_validator':'unsupported_validator','privacy':'store_class_mismatch','foreign_actor':'forbidden'}[problem]
    assert_error(code,revalidate,harness.workbench,harness.store,objects,run,validator)
    assert harness.row(run)==before and harness.event_rows(run)==events


def test_named_cli_revalidation_dispatches_explicit_target(monkeypatch,capsys):
    from analysis_agent import revalidation
    seen=[]
    monkeypatch.setattr(revalidation,'revalidate_command',lambda w,args:seen.append((args.run,args.validator,args.target_objects)) or {'status':'PASSED'})
    monkeypatch.setattr(sys,'argv',['workbench','--legacy-readonly','revalidate','--run','old','--validator','cell_validator@2','--target-db-url-file','target.url','--target-objects','target-objects'])
    cli.main(factory=lambda args:object())
    assert seen==[('old','cell_validator@2','target-objects')]
    assert json.loads(capsys.readouterr().out)['status']=='PASSED'


def test_schema_v3_and_archive_v2_have_distinct_admission():
    assert FakeMigrationProbe(version=3).invoke('check_version',expected_version=3)==3
    assert FakeMigrationProbe(version=2).invoke('check_version',legacy_readonly=True)==2
    with pytest.raises(WorkbenchError):FakeMigrationProbe(version=2).invoke('check_version')
