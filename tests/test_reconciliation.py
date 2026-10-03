"""Explicit operator reconciliation on synthetic data only."""
import asyncio
import json
import sys

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import insert, select, update

from analysis_agent.api import create_app
from analysis_agent.cli import main
from analysis_agent.contracts import Registry, WorkbenchError, digest
from analysis_agent.mcp_server import build_server
from analysis_agent.runtime import Workbench
from analysis_agent.storage import attempts, runs, steps
from test_runtime import harness, real_store, valid_report, assert_error


def interrupted(harness, monkeypatch):
    class Crash(BaseException): pass
    run_id = harness.create()
    execute = harness.adapter.execute
    def crash(*args): raise Crash()
    monkeypatch.setattr(harness.adapter, 'execute', crash)
    with pytest.raises(Crash): harness.call(run_id)
    monkeypatch.setattr(harness.adapter, 'execute', execute)
    return run_id, harness.workbench.reopen(run_id)['attempts'][0]['id']


def test_operator_reconciles_once_resolves_and_continues(harness, monkeypatch):
    run_id, attempt_id = interrupted(harness, monkeypatch)
    w = harness.workbench
    assert w.reconcile(run_id, older_than_seconds=3600)['count'] == 0
    assert w.reconcile(run_id, older_than_seconds=0)['reconciled'] == [attempt_id]
    count = len(harness.event_rows(run_id))
    assert w.reconcile(run_id, older_than_seconds=0)['count'] == 0
    assert len(harness.event_rows(run_id)) == count
    assert harness.row(run_id)['state'] == 'WAITING_RECONCILIATION'
    result = w.resolve(run_id, attempt_id, actor='Operator A', reason='Reviewed pinned read-only tool and crash marker')
    assert result['attempt_state'] == 'RESOLVED_FAILED' and result['state'] == 'RUNNING'
    record = w.reopen(run_id)
    resolution = record['resolutions'][0]
    assert resolution['actor'] == 'Operator A' and resolution['reason'] == 'Reviewed pinned read-only tool and crash marker'
    assert resolution['attempt_id'] == attempt_id and resolution['created_at']
    assert record['events'][-1]['kind'] == 'ATTEMPT_RESOLVED'
    assert_error('action_already_used', harness.call, run_id)
    item = harness.call(run_id, key='operator-approved-new-action')
    assert w.finalize(run_id, valid_report(item))['validation']['status'] == 'valid'
    assert [e['seq'] for e in harness.event_rows(run_id)] == list(range(1, len(harness.event_rows(run_id)) + 1))


@pytest.mark.parametrize('threshold', [-1, float('nan'), float('inf'), True, 'not-seconds'])
def test_reconciliation_requires_finite_nonnegative_operational_threshold(harness, threshold):
    run_id = harness.create()
    before = harness.event_rows(run_id)
    assert_error('invalid_reconcile_threshold', harness.workbench.reconcile, run_id, older_than_seconds=threshold)
    assert harness.event_rows(run_id) == before


@pytest.mark.parametrize('actor,reason', [('', 'basis'), ('  ', 'basis'), ('operator', ''), ('operator', ' '), ('x' * 201, 'basis'), ('operator', 'x' * 4001)])
def test_resolution_requires_named_operator_and_reason(harness, monkeypatch, actor, reason):
    run_id, attempt_id = interrupted(harness, monkeypatch)
    harness.workbench.reconcile(run_id, older_than_seconds=0)
    before = harness.event_rows(run_id)
    assert_error('invalid_resolution', harness.workbench.resolve, run_id, attempt_id, actor=actor, reason=reason)
    assert harness.event_rows(run_id) == before


def test_resolution_only_applies_to_unknown_attempt_on_same_run(harness, monkeypatch):
    run_id, attempt_id = interrupted(harness, monkeypatch)
    assert_error('invalid_state', harness.workbench.resolve, run_id, attempt_id, actor='operator', reason='basis')
    other = harness.create()
    assert_error('invalid_attempt', harness.workbench.resolve, other, attempt_id, actor='operator', reason='basis')
    harness.workbench.reconcile(run_id, older_than_seconds=0)
    assert_error('invalid_resolution', harness.workbench.resolve, run_id, attempt_id, actor='operator', reason='basis', disposition='accept_result')
    harness.workbench.resolve(run_id, attempt_id, actor='operator', reason='basis')
    before = harness.event_rows(run_id)
    assert_error('invalid_state', harness.workbench.resolve, run_id, attempt_id, actor='operator', reason='repeat')
    assert harness.event_rows(run_id) == before


def test_external_unknown_cannot_be_cleared_with_readonly_resolution(harness):
    spec = harness.adapter.tools['read_counter@2']
    harness.adapter.tools[spec.ref] = spec.model_copy(update={'side_effects': 'external', 'retry': 'never'})
    run_id = harness.create()
    harness.adapter.fail_execution = True
    assert_error('outcome_unknown', harness.call, run_id)
    attempt_id = harness.workbench.reopen(run_id)['attempts'][0]['id']
    before = harness.event_rows(run_id)
    assert_error('side_effects_external', harness.workbench.resolve, run_id, attempt_id, actor='operator', reason='Cannot assert no external effect')
    assert harness.event_rows(run_id) == before
    assert harness.row(run_id)['state'] == 'WAITING_RECONCILIATION'


def test_legacy_missing_side_effect_declaration_is_not_inferred(harness, monkeypatch):
    run_id, attempt_id = interrupted(harness, monkeypatch)
    harness.workbench.reconcile(run_id, older_than_seconds=0)
    manifest = harness.row(run_id)['manifest']
    pinned = harness.objects.get(manifest['tools_object'])
    del pinned['read_counter@2']['side_effects']
    manifest['tools_object'] = harness.objects.put(pinned)
    manifest['bindings']['tools'] = digest(pinned)
    with harness.store.engine.begin() as c:
        c.execute(update(runs).where(runs.c.id == run_id).values(manifest=manifest))
    assert_error('side_effects_unverified', harness.workbench.resolve, run_id, attempt_id, actor='operator', reason='Legacy declaration absent')


def test_resolution_does_not_unblock_another_unknown_attempt(harness, monkeypatch):
    run_id, first = interrupted(harness, monkeypatch)
    with harness.store.engine.begin() as c:
        a = dict(c.execute(select(attempts).where(attempts.c.id == first)).mappings().one())
        step = dict(c.execute(select(steps).where(steps.c.id == a['step_id'])).mappings().one())
        second, second_step = first + '-two', step['id'] + '-two'
        c.execute(insert(steps).values(**{**step, 'id': second_step, 'attempt_id': second, 'action_key': 'legacy-second'}))
        c.execute(insert(attempts).values(**{**a, 'id': second, 'step_id': second_step}))
    assert harness.workbench.reconcile(run_id, older_than_seconds=0)['count'] == 2
    assert harness.workbench.resolve(run_id, first, actor='operator', reason='first checked')['state'] == 'WAITING_RECONCILIATION'
    assert harness.workbench.resolve(run_id, second, actor='operator', reason='second checked')['state'] == 'RUNNING'


def test_cli_exposes_explicit_reconcile_and_resolution(harness, monkeypatch, capsys):
    run_id, attempt_id = interrupted(harness, monkeypatch)
    base = ['analysis-agent', '--db-url-file', 'not-opened-by-fake-factory', '--objects', 'unused']
    monkeypatch.setattr(sys, 'argv', base + ['reconcile', '--run', run_id, '--older-than-seconds', '0'])
    main(factory=lambda args: harness.workbench)
    assert json.loads(capsys.readouterr().out)['count'] == 1
    monkeypatch.setattr(sys, 'argv', base + ['resolutions', '--run', run_id, '--attempt', attempt_id, '--actor', 'CLI operator', '--reason', 'Synthetic recovery drill'])
    main(factory=lambda args: harness.workbench)
    assert json.loads(capsys.readouterr().out)['attempt_state'] == 'RESOLVED_FAILED'


def test_http_resolution_is_authenticated_and_mcp_cannot_resolve(harness, monkeypatch):
    run_id, attempt_id = interrupted(harness, monkeypatch)
    harness.workbench.reconcile(run_id, older_than_seconds=0)
    client = TestClient(create_app(harness.workbench, 'synthetic-test-token-with-more-than-32-characters'))
    url = f'/runs/{run_id}/resolutions'
    body = {'attempt_id': attempt_id, 'actor': 'HTTP operator', 'reason': 'Read-only declaration checked'}
    assert client.post(url, json=body).status_code == 401
    headers = {'Authorization': 'Bearer synthetic-test-token-with-more-than-32-characters'}
    assert client.post(url, json={**body, 'reason': ''}, headers=headers).status_code == 422
    assert client.post(url, json=body, headers=headers).json()['attempt_state'] == 'RESOLVED_FAILED'
    assert client.get(url, headers=headers).json()[0]['actor'] == 'HTTP operator'
    names = [tool.name for tool in asyncio.run(build_server(harness.workbench).list_tools())]
    assert not any('resol' in name or 'reconcile' in name for name in names)


def test_other_principal_cannot_reconcile_or_resolve(harness, monkeypatch):
    run_id, attempt_id = interrupted(harness, monkeypatch)
    stranger = Workbench(harness.store, harness.objects, Registry([], []), 'another-operator', {harness.project: [harness.adapter.ref]})
    assert_error('forbidden', stranger.reconcile, run_id, older_than_seconds=0)
    assert_error('forbidden', stranger.resolve, run_id, attempt_id, actor='pretend-owner', reason='Claimed ownership')


@pytest.mark.parametrize('classification', ['FAILED', 'REJECTED_AFTER_EXECUTION'])
def test_late_classification_does_not_bypass_another_unknown(harness, monkeypatch, classification):
    run_id, first = interrupted(harness, monkeypatch)
    with harness.store.engine.begin() as c:
        a = dict(c.execute(select(attempts).where(attempts.c.id == first)).mappings().one())
        step = dict(c.execute(select(steps).where(steps.c.id == a['step_id'])).mappings().one())
        second, second_step = first + '-late', step['id'] + '-late'
        c.execute(insert(steps).values(**{**step, 'id': second_step, 'attempt_id': second, 'action_key': 'late-action'}))
        c.execute(insert(attempts).values(**{**a, 'id': second, 'step_id': second_step}))
        c.execute(update(attempts).where(attempts.c.id == first).values(state='UNKNOWN'))
        c.execute(update(steps).where(steps.c.id == step['id']).values(state='UNKNOWN'))
        c.execute(update(runs).where(runs.c.id == run_id).values(state='WAITING_RECONCILIATION'))
    harness.workbench._record_tool_outcome(run_id, second_step, second, classification, 'tool_failed', 'Synthetic safe failure', retry='new_action_allowed')
    assert harness.row(run_id)['state'] == 'WAITING_RECONCILIATION'
    assert_error('invalid_state', harness.call, run_id, key='must-not-bypass-unknown')
    assert harness.workbench.resolve(run_id, first, actor='operator', reason='Last unknown checked')['state'] == 'RUNNING'


def test_resolution_rejects_unbound_tool_definition(harness, monkeypatch):
    run_id, attempt_id = interrupted(harness, monkeypatch)
    harness.workbench.reconcile(run_id, older_than_seconds=0)
    manifest = harness.row(run_id)['manifest']
    pinned = harness.objects.get(manifest['tools_object'])
    pinned['read_counter@2']['description'] = 'Changed without rebinding'
    manifest['tools_object'] = harness.objects.put(pinned)
    with harness.store.engine.begin() as c:
        c.execute(update(runs).where(runs.c.id == run_id).values(manifest=manifest))
    assert_error('version_mismatch', harness.workbench.resolve, run_id, attempt_id, actor='operator', reason='Unbound change must fail')


def test_unknown_attempt_blocks_dispatch_even_if_legacy_run_state_is_running(harness, monkeypatch):
    run_id, attempt_id = interrupted(harness, monkeypatch)
    harness.workbench.reconcile(run_id, older_than_seconds=0)
    with harness.store.engine.begin() as c:
        c.execute(update(runs).where(runs.c.id == run_id).values(state='RUNNING'))
    assert_error('unresolved_attempt', harness.call, run_id, key='legacy-state-bypass')
    assert harness.row(run_id)['tool_calls'] == 1
