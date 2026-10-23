from __future__ import annotations

import json
import os
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Literal

from fastapi import BackgroundTasks, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, Response

from .audit import verify_audit_chain
from .export import build_export, render_markdown
from .graph import MAX_PLAN_RETURNS, MAX_REVIEW_RETURNS, build_graph, resume_workflow, run_workflow
from .metrics import compute_metrics
from .schemas import ApprovalRequest, CreateRunRequest
from .settings import DEFAULT_CHECKPOINT
from .tool_registry import default_registry


@asynccontextmanager
async def lifespan(app: FastAPI):
    try:
        from langgraph.checkpoint.sqlite import SqliteSaver
    except ImportError as exc:
        raise RuntimeError(
            "工作流需要 langgraph-checkpoint-sqlite；请执行 "
            "python -m pip install -r workflow/requirements.txt"
        ) from exc
    checkpoint_path = Path(os.getenv("WORKFLOW_CHECKPOINT_PATH", str(DEFAULT_CHECKPOINT)))
    checkpoint_path.parent.mkdir(parents=True, exist_ok=True)
    registry = default_registry()
    with SqliteSaver.from_conn_string(str(checkpoint_path)) as checkpointer:
        app.state.workflow_registry = registry
        app.state.workflow_graph = build_graph(checkpointer, registry)
        yield


app = FastAPI(title="企业流程自动化多 Agent 平台", version="0.2.0", lifespan=lifespan)


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok", "service": "enterprise-workflow-platform"}


@app.get("/tools")
def list_tools(request: Request) -> dict[str, list[dict[str, Any]]]:
    return {"tools": request.app.state.workflow_registry.list_public()}


@app.post("/runs", status_code=202)
def create_run(
    body: CreateRunRequest,
    request: Request,
    background_tasks: BackgroundTasks,
) -> dict[str, str]:
    run_id = uuid.uuid4().hex
    config = {"configurable": {"thread_id": run_id}}
    initial_state = {"run_id": run_id, "task": body.task.strip()}
    background_tasks.add_task(
        run_workflow,
        request.app.state.workflow_graph,
        initial_state,
        config,
    )
    return {"run_id": run_id, "status": "running"}


PUBLIC_FIELDS = (
    "run_id", "task", "status", "error", "plan", "plan_approved", "plan_gate_reason",
    "plan_return_count", "step_policies", "tool_results", "source_excerpts",
    "review", "report", "support_review", "questions", "return_count",
    "approval_history", "llm_calls", "events",
)


def _public_state(state: dict[str, Any]) -> dict[str, Any]:
    public = {field: state[field] for field in PUBLIC_FIELDS if field in state}
    status = state.get("status")
    public["approval_stage"] = (
        "plan" if status == "awaiting_plan_approval"
        else "report" if status == "awaiting_human"
        else None
    )
    return public


def _state_or_404(request: Request, run_id: str) -> dict[str, Any]:
    config = {"configurable": {"thread_id": run_id}}
    try:
        snapshot = request.app.state.workflow_graph.get_state(config)
    except Exception as exc:
        raise HTTPException(status_code=404, detail="任务不存在") from exc
    values = snapshot.values or {}
    if not values:
        raise HTTPException(status_code=404, detail="任务不存在")
    return values


@app.get("/runs/{run_id}")
def get_run(run_id: str, request: Request) -> dict[str, Any]:
    config = {"configurable": {"thread_id": run_id}}
    try:
        snapshot = request.app.state.workflow_graph.get_state(config)
    except Exception as exc:
        raise HTTPException(status_code=404, detail="任务不存在") from exc
    values = snapshot.values or {}
    if not values:
        return {"run_id": run_id, "status": "running", "events": []}
    return _public_state(values)


@app.post("/runs/{run_id}/approval", status_code=202)
def approve_run(
    run_id: str,
    body: ApprovalRequest,
    request: Request,
    background_tasks: BackgroundTasks,
) -> dict[str, str]:
    config = {"configurable": {"thread_id": run_id}}
    try:
        snapshot = request.app.state.workflow_graph.get_state(config)
    except Exception as exc:
        raise HTTPException(status_code=404, detail="任务不存在") from exc
    state = snapshot.values or {}
    status = state.get("status")
    if status not in {"awaiting_plan_approval", "awaiting_human"}:
        raise HTTPException(status_code=409, detail="任务当前不在人工审核状态")
    stage = "plan" if status == "awaiting_plan_approval" else "report"
    if stage == "plan":
        count = state.get("plan_return_count", 0)
        limit = MAX_PLAN_RETURNS
        limit_detail = "计划退回次数已达到上限"
    else:
        count = state.get("return_count", 0)
        limit = MAX_REVIEW_RETURNS
        limit_detail = "退回次数已达到上限"
    if body.decision == "return" and count + 1 > limit:
        raise HTTPException(status_code=409, detail=limit_detail)
    background_tasks.add_task(
        resume_workflow,
        request.app.state.workflow_graph,
        config,
        body.model_dump(),
    )
    return {
        "run_id": run_id,
        "status": "running",
        "decision": body.decision,
        "approval_stage": stage,
    }


@app.get("/runs/{run_id}/audit/verify")
def verify_run_audit(run_id: str, request: Request) -> dict[str, Any]:
    return verify_audit_chain(_state_or_404(request, run_id).get("events", []))


@app.get("/runs/{run_id}/metrics")
def get_run_metrics(run_id: str, request: Request) -> dict[str, Any]:
    return compute_metrics(_state_or_404(request, run_id))


@app.get("/runs/{run_id}/export")
def export_run(
    run_id: str,
    request: Request,
    format: Literal["md", "json"] = "md",
) -> Response:
    trace = build_export(_state_or_404(request, run_id))
    if format == "md":
        content = render_markdown(trace)
        media_type = "text/markdown; charset=utf-8"
        filename = f"workflow_{run_id}.md"
    else:
        content = json.dumps(trace, ensure_ascii=False, indent=2)
        media_type = "application/json"
        filename = f"workflow_{run_id}.json"
    return Response(
        content=content.encode("utf-8"),
        media_type=media_type,
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@app.exception_handler(Exception)
async def unexpected_error_handler(_: Request, exc: Exception) -> JSONResponse:
    return JSONResponse(status_code=500, content={"detail": f"工作流服务错误：{str(exc)[:300]}"})


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("workflow.api:app", host="127.0.0.1", port=8001, reload=False)
