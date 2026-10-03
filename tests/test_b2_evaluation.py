import pytest
from analysis_agent.evaluation import EvaluationLog
from analysis_agent.contracts import WorkbenchError
from test_runtime import harness, real_store


def test_evaluation_receipts_are_immutable_and_reopenable(harness):
    log = EvaluationLog(harness.workbench, harness.project)
    first = log.append('trial', 'one', {'correct': False, 'status': 'FAILED'})
    assert log.append('trial', 'one', {'correct': False, 'status': 'FAILED'}) == first
    assert EvaluationLog(harness.workbench, harness.project).read('trial')[0]['payload']['correct'] is False
    with pytest.raises(WorkbenchError, match='different content'):
        log.append('trial', 'one', {'correct': True, 'status': 'PASSED'})
    assert len(log.read()) == 1


def test_evaluation_receipts_do_not_expose_other_scopes_or_answers(harness):
    with pytest.raises(WorkbenchError): EvaluationLog(harness.workbench, 'another')
    log = EvaluationLog(harness.workbench, harness.project)
    with pytest.raises(WorkbenchError): log.append('trial', 'bad', {'nested': {'reference_answers': [1]}})
    with pytest.raises(WorkbenchError): log.append('trial', 'oracle', {'necessary_facts': []})
    assert log.read() == []
    assert not any('evaluation' in ref for ref in harness.adapter.tools)
