"""Disposable public demo. Real providers are optional and never used by tests."""
from __future__ import annotations
import argparse
import contextlib
import json
import os
from pathlib import Path
import secrets
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import uuid
import webbrowser

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / 'src'), str(ROOT / 'tests')]
from sqlalchemy import text
from sqlalchemy.engine import URL
from analysis_agent.api import create_app
from analysis_agent.contracts import Registry, ResultSpec, RequiredFact, RunRequest, WorkbenchError
from analysis_agent.demo import CommerceAdapter, demo_skill, default_parameters
from analysis_agent.execution import Executor, ExecutionConfig, BatchConfig
from analysis_agent.providers import FixtureProvider, GrokProvider, DeepSeekProvider
from analysis_agent.runtime import Workbench
from analysis_agent.storage import Objects, Store, SCHEMA_VERSION
import uvicorn


def fixture_response(payload):
    context = json.loads(payload['messages'][1]['content'])
    accepted = context['accepted_evidence']
    if not accepted:
        return {'action': 'query', 'tool_ref': 'compare@2', 'args': {},
                'explanation': 'Read both fixed synthetic cohorts.'}
    item = next(e for e in accepted if e['tool_ref'] == 'compare@2')
    facts = []
    for row, reference in zip(item['rows'], item['row_references']):
        for column in ('orders', 'refunded_orders', 'refund_rate'):
            facts.append({'schema_version': 'fact@2', 'operation': 'cell',
                          'inputs': [{'evidence_id': item['evidence_id'], **reference, 'column': column}],
                          'value': row[column], 'unit': item['units'][column]})
    return {'action': 'report', 'report': {'result_status': 'complete',
            'title': 'Synthetic cohort comparison · fixture, not a model evaluation', 'facts': facts,
            'limitations': ['Deterministic fixture provider; no model-quality inference.']}}


def bench(store, state):
    adapter = CommerceAdapter('anomaly')
    Objects.initialize(state / 'objects', 'public')
    return Workbench(store, Objects(state / 'objects'), Registry([adapter], [demo_skill()]),
                     'public-demo', {'demo': [adapter.ref]}, entrypoints=[__file__])


def execution(wb, provider=None, config=None):
    provider = provider or FixtureProvider(fixture_response)
    worker = Executor(wb, provider)
    if config is None:
        batch = BatchConfig(provider='fixture', model='fixture@1', returned_models=('fixture@1',),
                            currency='fixture', input_per_million='0', output_per_million='0',
                            pricing_source='deterministic fixture', pricing_date='2026-10-02', cost_micros=100000)
        config = ExecutionConfig(provider='fixture', model='fixture@1', batch_account='demo-fixture',
                                 max_calls=4, max_output_tokens=1024, max_input_tokens=50000,
                                 max_total_output_tokens=4096, max_cost_micros=100000, wall_seconds=180)
    else:
        batch = BatchConfig.model_validate(config['batch'])
        config = ExecutionConfig.model_validate(config['execution'])
        if batch.provider != config.provider or batch.model != config.model:
            raise ValueError('Real provider configuration must identify one consistent model')
        if batch.cost_micros > 500000 or config.max_cost_micros > 500000 or config.max_calls > 4:
            raise ValueError('Optional demo is capped at 0.50 currency units and four calls')
    worker.register_batch(config.batch_account, batch)
    return worker, config


def task(wb):
    return RunRequest(project_id='demo', source_ref=wb.projects['demo'][0], skill_ref=demo_skill().ref,
                      question='Compare the fixed synthetic cohorts; cite counts and refund rates.',
                      parameters=default_parameters(), tool_budget=6, report_budget=3,
                      result_spec=ResultSpec(requirements=[RequiredFact(column=column, entity={'window': window})
                          for window in ('current', 'baseline')
                          for column in ('orders', 'refunded_orders', 'refund_rate')]))


def publish_demo(worker, config):
    rid = worker.submit(task(worker.w), 'demo-' + uuid.uuid4().hex, config)['run_id']
    result = worker.run('demo-worker', rid)
    if result.get('run', {}).get('state') != 'SUCCEEDED' or result['report']['validation']['status'] == 'blocked':
        raise RuntimeError('Demo did not publish an accepted report')
    return {'run_id': rid, 'state': result['run']['state'], 'validation': result['report']['validation']['status'],
            'result_status': result['report']['report']['result_status'], 'fixture': worker.provider.fixture,
            'facts': len(result['report']['validation']['normalized_facts'])}


def optional_real(wb, fixture_only):
    if fixture_only:
        return {'status': 'NOT RUN', 'reason': 'fixture-only invocation'}
    available = [name for name, key in [('deepseek','DEEPSEEK_API_KEY'),('xai','XAI_API_KEY')] if os.environ.get(key)]
    if not available:
        return {'status': 'NOT RUN', 'reason': 'No provider key configured'}
    filename = os.environ.get('DEMO_REAL_CONFIG')
    if not filename:
        return {'status': 'NOT RUN', 'reason': 'Key detected; set DEMO_REAL_CONFIG with model, dated rates and budget to opt in'}
    config = json.loads(Path(filename).read_text())
    provider_name = config['execution']['provider']
    if provider_name not in available:
        raise ValueError('Configured real provider has no environment credential')
    worker, policy = execution(wb, DeepSeekProvider() if provider_name == 'deepseek' else GrokProvider(), config)
    try:
        return {'status': 'PASSED', **publish_demo(worker, policy)}
    finally:
        worker.close()


def credential(state):
    filename = state / 'api.token'
    if not filename.exists():
        fd = os.open(filename, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, 'w') as handle:
            handle.write(secrets.token_urlsafe(48))
    return filename.read_text().strip()


def queue_loop(worker, stop):
    while not stop.is_set():
        for row in worker.w.history('demo'):
            if row['state'] in {'ADMITTED', 'RUNNING'}:
                try:
                    worker.run('demo-queue', row['id'])
                except WorkbenchError as error:
                    print(json.dumps({'worker_error': error.code}), flush=True)
        stop.wait(0.5)


class LocalDemo:
    def __init__(self, test_instance=False):
        import local_postgres
        self.testing = test_instance
        self.state = (ROOT.parent.parent / 'work/b2r_test_pg') if test_instance else Path(tempfile.mkdtemp(prefix='awb-demo-'))
        if test_instance and self.state.exists():
            raise RuntimeError('R test directory already exists; refuse to reuse or erase it implicitly')
        local_postgres.DATABASE = 'workbench_test_b2r' if test_instance else 'workbench_demo'
        self.pg = local_postgres.LocalPostgres(self.state / 'pg')
        self.store = None

    def start(self):
        with self.pg.lock():
            self.pg.start()
        if self.testing:
            from database_guard import open_test_store, migrate_test_store
            self.store = open_test_store(self.pg.url_file)
            assert self.store._approved_test_database == 'workbench_test_b2r'
            migrate_test_store(self.store, from_version=0, to_version=SCHEMA_VERSION)
        else:
            self.store = Store(self.pg.url())
            with self.store.engine.connect() as connection:
                db, directory = connection.execute(text("SELECT current_database(), current_setting('data_directory')")).one()
                if db != 'workbench_demo' or Path(directory).resolve() != self.pg.state:
                    raise RuntimeError('Disposable demo database identity mismatch')
            self.store.migrate(expected_database='workbench_demo', from_version=0, to_version=SCHEMA_VERSION)
        return self.store

    def close(self):
        if self.store:
            self.store.engine.dispose()
        with self.pg.lock():
            self.pg.stop()
            if self.pg.running():
                raise RuntimeError('Database still running; do not remove state')
        if self.state.exists():
            shutil.rmtree(self.state)
        if self.pg.socket.exists():
            shutil.rmtree(self.pg.socket)


def local(args):
    demo = LocalDemo(args.test_instance)
    worker = None
    gateway = None
    stop = threading.Event()
    thread = None
    try:
        wb = bench(demo.start(), demo.state)
        worker, config = execution(wb)
        summary = {'fixture': publish_demo(worker, config), 'real': optional_real(wb, args.fixture_only)}
        token = credential(demo.state)
        (demo.state / 'demo-summary.json').write_text(json.dumps(summary, indent=2))
        if args.summary:
            Path(args.summary).write_text(json.dumps(summary, indent=2))
        env = {**os.environ, 'WORKBENCH_UI_TOKEN_FILE': str(demo.state / 'api.token'),
               'WORKBENCH_UI_API_ORIGIN': 'http://127.0.0.1:8911', 'WORKBENCH_UI_PORT': '8912'}
        gateway = subprocess.Popen(['node', str(ROOT / 'scripts/demo_gateway.mjs')], cwd=ROOT, env=env)
        thread = threading.Thread(target=queue_loop, args=(worker, stop), daemon=True)
        thread.start()
        app = create_app(wb, token, executor=worker, execution_config=config)
        server = uvicorn.Server(uvicorn.Config(app, host='127.0.0.1', port=8911, log_level='warning'))
        # Own handlers avoid Uvicorn re-emitting SIGTERM before our cleanup runs.
        server.capture_signals = contextlib.nullcontext
        for sig in (signal.SIGINT, signal.SIGTERM):
            signal.signal(sig, lambda *_: setattr(server, 'should_exit', True))
        print(json.dumps({'status': 'PASSED', 'fixture': summary['fixture'], 'ui': 'http://127.0.0.1:8912',
                          'notice': 'Fixture, not a model evaluation. Stop with Ctrl-C to remove demo state.'}), flush=True)
        if not args.no_open:
            threading.Timer(1, lambda: webbrowser.open('http://127.0.0.1:8912')).start()
        server.run()
    finally:
        stop.set()
        if thread:
            thread.join(timeout=10)
        if gateway:
            gateway.terminate()
            try:
                gateway.wait(timeout=10)
            except subprocess.TimeoutExpired:
                gateway.kill(); gateway.wait()
        if worker:
            worker.close()
        demo.close()
        print('PASSED: disposable demo database, objects, token and socket removed', flush=True)


def container_store(state, initialize=False):
    # This entry point is only for the unpublished, isolated Compose demo.
    password = Path('/run/secrets/db_password').read_text().strip()
    store = Store(URL.create('postgresql+psycopg', username='demo', password=password,
                             host='postgres', port=5432, database='workbench_demo'))
    with store.engine.connect() as connection:
        if connection.execute(text('SELECT current_database()')).scalar_one() != 'workbench_demo':
            raise RuntimeError('Compose demo database identity mismatch')
    if initialize and store.read_version() == 0:
        store.migrate(expected_database='workbench_demo', from_version=0, to_version=SCHEMA_VERSION)
    store.check_version()
    return store


def container(args):
    state = Path('/state')
    state.mkdir(parents=True, exist_ok=True)
    if args.command == 'api':
        store = container_store(state, initialize=True)
        wb = bench(store, state); worker, config = execution(wb)
        token = credential(state)
        (state / 'ready').write_text('ready')
        uvicorn.run(create_app(wb, token, executor=worker, execution_config=config), host='0.0.0.0', port=8911, log_level='warning')
    else:
        while not (state / 'ready').exists():
            time.sleep(1)
        store = container_store(state); wb = bench(store, state); worker, config = execution(wb)
        summary = publish_demo(worker, config)
        (state / 'demo-summary.json').write_text(json.dumps(summary))
        queue_loop(worker, threading.Event())


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=['local', 'api', 'worker', 'prepare-compose'])
    parser.add_argument('--fixture-only', action='store_true')
    parser.add_argument('--test-instance', action='store_true')
    parser.add_argument('--no-open', action='store_true')
    parser.add_argument('--summary')
    args = parser.parse_args()
    if args.command == 'prepare-compose':
        state = Path(os.environ['DEMO_CONTAINER_STATE']).resolve()
        if ROOT == state or ROOT in state.parents:
            raise RuntimeError('Keep generated container credentials outside the checkout')
        state.mkdir(parents=True, mode=0o700, exist_ok=True)
        target = state / 'db-password'
        fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, 'w') as handle:
            handle.write(secrets.token_urlsafe(32))
        print('PASSED: local Compose credential created')
    elif args.command == 'local':
        local(args)
    else:
        container(args)

if __name__ == '__main__':
    try:
        main()
    except Exception as error:
        print(json.dumps({'status': 'FAILED', 'code': getattr(error, 'code', type(error).__name__),
                          'message': 'Demo failed; connection and credential details suppressed'}), file=sys.stderr)
        raise SystemExit(1) from None
