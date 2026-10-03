# Running the synthetic demo

The default workflow is local, disposable, and single-principal. Prerequisites and pinned installation commands are in the root README. `scripts/demo.sh --fixture-only` never calls a paid provider. It uses a real isolated PostgreSQL process and the same server-side executor as the provider adapters, with an explicitly labelled deterministic fixture.

Use `--no-open` for a headless machine. `DEMO_PYTHON` may name a separately installed Python environment. The demo refuses to reuse an existing R test directory and removes only the instance it owns. SIGINT/SIGTERM stop the API, worker and gateway before PostgreSQL cleanup. Do not forcibly kill a process while it is writing; if interrupted externally, inspect the owned temporary instance before deleting anything.

## Optional real provider

This path is **NOT RUN** in release-preparation validation. Provide one environment credential (`DEEPSEEK_API_KEY` or `XAI_API_KEY`) and a local JSON file referenced by `DEMO_REAL_CONFIG`. Never put credentials in that file, an argument, frontend configuration, or the repository. The JSON contains `batch` and `execution` objects defined by `BatchConfig` and `ExecutionConfig` in `src/analysis_agent/execution.py`.

Choose the model, returned-model aliases, currency, a dated official pricing URL, input/output rates, token limits and cost limit explicitly. The demo additionally rejects more than four calls or a budget above 0.50 currency units. Omit `--fixture-only` to append exactly one real run after the fixture. A configured key alone leaves the paid run NOT RUN until the explicit model/rate/budget configuration is supplied. An exhausted budget or uncertain provider result is retained as a failure/uncertain record, never retried to disguise the result.

## Optional containers

**Startup validation: NOT RUN. Docker was not installed on the preparation machine.** Do not install privileged desktop software as part of this demo.

The four Compose services are PostgreSQL, API, worker and web. Only the web service publishes a loopback port. All data is synthetic; Compose uses disposable named volumes and a generated database password outside the checkout.

```sh
export DEMO_CONTAINER_STATE="$(mktemp -d)"
.venv/bin/python scripts/demo_runtime.py prepare-compose
docker compose up --build
# Open http://127.0.0.1:8912
# When finished:
docker compose down --volumes
rm "$DEMO_CONTAINER_STATE/db-password"
rmdir "$DEMO_CONTAINER_STATE"
```

The images have exact version tags, but were not pulled or inspected here; digests and Linux installation still require validation. Container credentials and volumes must never enter the release directory.

## Recording a local demonstration

After the demo starts, install the locked Playwright Chromium dependency and run the recording script. The script writes a WebM recording and local screenshots; convert the recording to MP4 for the README. Browser recording uses synthetic data and no remote page resources.

```sh
cd apps/web
npx playwright install chromium
cd ../..
node scripts/demo_record.mjs --summary /path/to/demo-summary.json --output /path/to/recording
```
