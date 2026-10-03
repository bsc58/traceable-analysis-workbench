"""Batch allow-list pairing and tamper-resistant evidence checks; no DB writes."""
from copy import deepcopy
import importlib.util
from pathlib import Path
import json
import pytest
from database_guard import APPROVED_TEST_INSTANCES,open_test_store,DatabaseGuardError
from test_database_guard import FakeStore,fake_url_file

@pytest.mark.parametrize('database',list(APPROVED_TEST_INSTANCES))
@pytest.mark.parametrize('directory',list(APPROVED_TEST_INSTANCES.values()))
def test_only_matching_test_instance_pairs_are_allowed(fake_url_file,database,directory):
    store=FakeStore(database,directory=str(directory/'pg'))
    if APPROVED_TEST_INSTANCES[database]==directory:
        assert open_test_store(fake_url_file,store_factory=lambda _:store) is store
    else:
        with pytest.raises(DatabaseGuardError):open_test_store(fake_url_file,store_factory=lambda _:store)

@pytest.mark.parametrize('database',['WORKBENCH_TEST_B1MAIN','WORKBENCH_TEST_B1P7','WORKBENCH_TEST_B1P9A','workbench_test_p3'])
def test_case_variants_and_previous_batch_fail_closed(fake_url_file,database):
    with pytest.raises(DatabaseGuardError):open_test_store(fake_url_file,store_factory=lambda _:FakeStore(database))


def verifier():
    spec=importlib.util.spec_from_file_location('phase_verifier',Path(__file__).resolve().parents[1]/'scripts/verify_phase.py')
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module);return module

@pytest.mark.parametrize('tamper',['none','source','output','history','missing_group','wrong_source','unclean','acceptance','preseal','skipped'])
def test_phase_verifier_detects_incomplete_or_changed_evidence(tmp_path,tamper):
    m=verifier();root=tmp_path/'src';root.mkdir();code=root/'engine.py';code.write_text('value=1\n')
    hist=tmp_path/'history.txt';hist.write_text('immutable');output=tmp_path/'out.txt';output.write_text('1 passed')
    acceptance=tmp_path/'acceptance.json';acceptance.write_text(json.dumps({'cases':[{'status':'NOT RUN'} for _ in range(58)]}))
    manifest={'phase':'B0','source_roots':[str(root)],'files':m.inventory([root]),'sealed_at':'2026-01-01','required_groups':['test'],'acceptance_file':str(acceptance),'test_instance_directories':[str(tmp_path/'test_instance')]}
    manifest['source_sha256']=m.canonical_hash(manifest['files'])
    record={'group':'test','exit_code':0,'status':'PASSED','source_sha256':manifest['source_sha256'],'started_at':'2026-01-02','output_file':str(output),'output_sha256':m.checksum(output)}
    protected={'files':{str(hist):m.checksum(hist)}};records=[record]
    if tamper=='source':code.write_text('value=2')
    elif tamper=='output':output.write_text('changed')
    elif tamper=='history':hist.write_text('changed')
    elif tamper=='missing_group':records=[]
    elif tamper=='wrong_source':record['source_sha256']='wrong'
    elif tamper=='unclean':(tmp_path/'test_instance').mkdir()
    elif tamper=='acceptance':acceptance.write_text(json.dumps({'cases':[]}))
    elif tamper=='preseal':record['started_at']='2025-12-31'
    elif tamper=='skipped':record['skipped']=1
    assert m.verify(manifest,records,protected)['status']==('PASSED' if tamper=='none' else 'FAILED')
