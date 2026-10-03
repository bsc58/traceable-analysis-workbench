"""Local single-principal API. Multi-user/remote OAuth is outside this slice."""
import secrets
from typing import Literal

from fastapi import Depends, FastAPI, Header, Request
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from .contracts import Report, RunRequest, WorkbenchError
from .presentation import render_record


class Action(BaseModel):
    model_config = ConfigDict(extra="forbid")
    tool_ref: str
    args: dict
    action_key: str = Field(min_length=1, max_length=128)
    explanation: str = Field(max_length=4000)


class ResolutionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    attempt_id: str = Field(min_length=1, max_length=128)
    actor: str = Field(min_length=1, max_length=200)
    reason: str = Field(min_length=1, max_length=4000)
    disposition: Literal["confirm_no_side_effects_failed"] = "confirm_no_side_effects_failed"


def create_app(workbench, token: str, *, executor=None, execution_config=None):
    if len(token) < 32:
        raise ValueError("Local API requires a random token of at least 32 characters")

    def authorized(authorization: str = Header(default="")):
        if not secrets.compare_digest(authorization, "Bearer " + token):
            raise WorkbenchError("unauthorized", "A local API credential is required", 401)

    app = FastAPI(title="Traceable Analysis Workbench", version="0.1.0", dependencies=[Depends(authorized)],
                  docs_url=None, redoc_url=None, openapi_url=None)

    app.state.workbench = workbench
    from . import api_ui

    @app.get("/ui/projects/{project_id}/configuration")
    def ui_configuration(project_id: str, request: Request):
        result = api_ui.configuration(project_id, request)
        if executor is not None:
            config = execution_config.model_dump(mode="json") if hasattr(execution_config,"model_dump") else dict(execution_config)
            result["budgets"]["model_execution"] = config
            result["scope"] = "本机固定身份；内部执行器队列；模型与预算由服务端绑定，worker另行领取任务"
        return result

    app.include_router(api_ui.router)

    @app.exception_handler(WorkbenchError)
    async def domain_error(request, exc):
        return JSONResponse(status_code=exc.status, content={"error": {"code": exc.code, "message": exc.message}})

    @app.get("/health")
    def health():
        return {"status": "alive", "readiness": "not_evaluated"}

    @app.post("/runs", status_code=202)
    def create(request: RunRequest, idempotency_key: str = Header()):
        return (executor.submit(request, idempotency_key, execution_config) if executor is not None
                else workbench.create(request, idempotency_key))

    @app.get("/projects/{project_id}/runs")
    def history(project_id: str):
        return workbench.history(project_id)

    @app.get("/projects/{project_id}/capabilities")
    def capabilities(project_id: str):
        workbench._authorize(project_id)
        return {ref: workbench.registry.adapters[ref].capabilities() for ref in workbench.projects[project_id]}

    @app.get("/runs/{run_id}")
    def reopen(run_id: str):
        return workbench.reopen(run_id)

    @app.get("/runs/{run_id}/skill")
    def skill(run_id: str, attachment: str | None = None):
        return workbench.get_run_skill(run_id, attachment)

    @app.get("/runs/{run_id}/events")
    def event_stream(run_id: str, after: int = 0):
        return [e for e in workbench.reopen(run_id)["events"] if e["seq"] > after]

    @app.post("/runs/{run_id}/actions")
    def action(run_id: str, request: Action):
        return workbench.call(run_id, request.tool_ref, request.args, request.action_key, request.explanation)

    @app.post("/runs/{run_id}/report")
    def finalize(run_id: str, report: Report):
        result = workbench.finalize(run_id, report)
        return JSONResponse(result, status_code=422 if result["validation"]["status"] == "blocked" else 200)

    @app.post("/runs/{run_id}/resolutions")
    def resolution(run_id: str, request: ResolutionRequest):
        return workbench.resolve(run_id, request.attempt_id, actor=request.actor,
                                 reason=request.reason, disposition=request.disposition)

    @app.get("/runs/{run_id}/resolutions")
    def resolution_history(run_id: str):
        return workbench.reopen(run_id)["resolutions"]

    @app.get("/runs/{run_id}/evidence/{evidence_id}")
    def read_evidence(run_id: str, evidence_id: str):
        return workbench.read_evidence(run_id, evidence_id)

    @app.get("/runs/{run_id}/view", response_class=HTMLResponse)
    def view(run_id: str):
        return HTMLResponse(render_record(workbench.reopen(run_id)), headers={
            "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; frame-ancestors 'none'",
            "X-Content-Type-Options": "nosniff", "Cache-Control": "no-store"})

    @app.post("/runs/{run_id}/{operation}")
    def unsupported(run_id: str, operation: str):
        with workbench.store.engine.connect() as c:
            row = workbench._run(c, run_id)
        if executor is not None and row["manifest"]["execution"]["driver"] == "internal_runner":
            if operation == "cancel": return executor.cancel(run_id)
            if operation == "resume": return executor.resume(run_id)
        return workbench.unsupported(operation)

    return app
