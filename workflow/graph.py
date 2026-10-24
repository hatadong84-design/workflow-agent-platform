from __future__ import annotations

from datetime import datetime, timezone
from typing import Annotated, Any, TypedDict
from uuid import uuid4

from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt

from . import agents
from .audit import chain_events
from .retrieval import source_excerpts
from .schemas import WorkflowPlan
from .support import evaluate_report_support
from .tool_registry import (
    ToolRegistry,
    canonical_cache_key,
    default_registry,
    plan_approval_reason,
    policy_check,
    resolve_references,
)


MAX_REVIEW_RETURNS = 2
MAX_PLAN_RETURNS = 2


class ExecutionHalt(RuntimeError):
    def __init__(self, message: str, results: dict, cache: dict, events: list[dict]) -> None:
        super().__init__(message)
        self.results = results
        self.cache = cache
        self.events = events


class ToolExecutionError(ExecutionHalt):
    pass


class PolicyDeniedError(ExecutionHalt):
    pass


class ApprovalRequiredError(ExecutionHalt):
    pass


class WorkflowState(TypedDict, total=False):
    run_id: str
    task: str
    status: str
    error: str
    failed_node: str
    error_category: str
    plan: dict[str, Any]
    plan_approved: bool
    plan_gate_reason: str
    plan_return_count: int
    step_policies: dict[str, dict[str, Any]]
    tool_results: dict[str, dict[str, Any]]
    tool_cache: dict[str, dict[str, Any]]
    source_excerpts: dict[str, str]
    review: dict[str, Any]
    report: dict[str, Any]
    support_review: dict[str, Any]
    questions: list[str]
    previous_feedback: str
    approval_decision: str
    return_count: int
    approval_history: list[dict[str, Any]]
    llm_calls: list[dict[str, Any]]
    events: Annotated[list[dict[str, Any]], chain_events]


def _event(kind: str, status: str, detail: str = "", **extra: Any) -> dict[str, Any]:
    return {
        "event_id": uuid4().hex,
        "kind": kind,
        "status": status,
        "detail": detail[:500],
        "at": datetime.now(timezone.utc).isoformat(),
        **extra,
    }


def _topological_steps(plan: WorkflowPlan) -> list:
    by_id = {step.step_id: step for step in plan.steps}
    ordered = []
    complete: set[str] = set()
    while len(ordered) < len(plan.steps):
        ready = [
            step for step in plan.steps
            if step.step_id not in complete and set(step.depends_on).issubset(complete)
        ]
        if not ready:
            raise ValueError("计划依赖图无法拓扑排序")
        for step in ready:
            ordered.append(step)
            complete.add(step.step_id)
    return ordered


def _current_excerpts(
    plan_dict: dict[str, Any],
    results: dict[str, dict[str, Any]],
) -> dict[str, str]:
    step_ids = {step.get("step_id") for step in plan_dict.get("steps", [])}
    current = {step_id: result for step_id, result in results.items() if step_id in step_ids}
    return source_excerpts(current.values())


def execute_plan(
    plan_dict: dict[str, Any],
    registry: ToolRegistry,
    previous_results: dict[str, dict[str, Any]] | None = None,
    previous_cache: dict[str, dict[str, Any]] | None = None,
    plan_approved: bool = False,
) -> tuple[dict[str, dict], dict[str, dict], list[dict]]:
    plan = WorkflowPlan.model_validate(plan_dict)
    results = dict(previous_results or {})
    cache = dict(previous_cache or {})
    events: list[dict] = []
    for step in _topological_steps(plan):
        decision = policy_check(step, registry)
        events.append(_event(
            "policy_check",
            decision.action,
            decision.reason,
            step_id=step.step_id,
            tool_name=step.tool_name,
            risk_level=decision.risk_level,
            data_scope=decision.data_scope,
            action=decision.action,
        ))
        if decision.action == "deny":
            raise PolicyDeniedError(decision.reason, results, cache, events)
        if decision.action == "needs_approval" and not plan_approved:
            raise ApprovalRequiredError(decision.reason, results, cache, events)

        dependency_results = {dep: results[dep] for dep in step.depends_on if dep in results}
        if set(dependency_results) != set(step.depends_on):
            raise ValueError(f"步骤 {step.step_id} 的依赖结果缺失")
        if any(results[dep].get("_skipped") for dep in step.depends_on):
            results[step.step_id] = {"_skipped": True, "reason": "dependency step was skipped"}
            events.append(_event(
                "tool_skipped", "skipped", "依赖步骤已被跳过，后续步骤不再执行", step_id=step.step_id
            ))
            continue
        try:
            arguments = resolve_references(step.arguments, dependency_results)
        except IndexError:
            # Empty search results make a planned "read first match" step inapplicable.
            # Record it as skipped and let Review explain the evidence gap.
            results[step.step_id] = {"_skipped": True, "reason": "dependency returned no matching item"}
            events.append(_event(
                "tool_skipped", "skipped", "依赖步骤没有可读取的匹配项", step_id=step.step_id
            ))
            continue
        validated = registry.validate_call(step.tool_name, arguments)
        arguments = validated.model_dump()
        cache_key = canonical_cache_key(step.tool_name, arguments)
        events.append(_event("tool_started", "running", f"执行工具 {step.tool_name}", step_id=step.step_id))
        if cache_key in cache:
            output = cache[cache_key]
            events.append(_event("tool_cached", "completed", "复用相同工具参数的已完成结果", step_id=step.step_id))
        else:
            def retry_event(attempt: int, error: str) -> None:
                events.append(_event(
                    "tool_retry", "retrying", error, step_id=step.step_id,
                    attempt=attempt,
                ))

            try:
                output = registry.invoke(step.tool_name, arguments, on_retry=retry_event)
            except Exception as exc:
                events.append(_event(
                    "tool_failed", "failed", str(exc), step_id=step.step_id,
                    tool_name=step.tool_name,
                ))
                raise ToolExecutionError(str(exc), results, cache, events) from exc
            # Tools return excerpts and stable identifiers only; full documents stay on disk.
            cache[cache_key] = output
        results[step.step_id] = output
        events.append(_event("tool_completed", "completed", f"工具 {step.tool_name} 已完成", step_id=step.step_id))
    return results, cache, events


def build_graph(checkpointer, registry: ToolRegistry | None = None):
    registry = registry or default_registry()
    builder = StateGraph(WorkflowState)
    builder.add_node("initialize", initialize_node)
    builder.add_node("planner", lambda state: planner_node(state, registry))
    builder.add_node("plan_approval", plan_approval_node)
    builder.add_node("data_agent", lambda state: data_agent_node(state, registry))
    builder.add_node("review_agent", review_agent_node)
    builder.add_node("human_approval", human_approval_node)
    builder.add_node("report_agent", report_agent_node)
    builder.add_edge(START, "initialize")
    builder.add_edge("initialize", "planner")
    builder.add_conditional_edges(
        "planner",
        route_after_planner,
        {"plan_approval": "plan_approval", "data_agent": "data_agent", "end": END},
    )
    builder.add_conditional_edges(
        "plan_approval",
        route_after_plan_approval,
        {"planner": "planner", "data_agent": "data_agent", "end": END},
    )
    builder.add_conditional_edges("data_agent", route_after_data, {"review_agent": "review_agent", "end": END})
    builder.add_conditional_edges(
        "review_agent",
        route_after_review,
        {"human_approval": "human_approval", "end": END},
    )
    builder.add_conditional_edges(
        "human_approval",
        route_after_approval,
        {"planner": "planner", "report_agent": "report_agent", "end": END},
    )
    builder.add_edge("report_agent", END)
    return builder.compile(checkpointer=checkpointer)


def initialize_node(state: WorkflowState) -> dict[str, Any]:
    return {
        "status": "running",
        "plan_approved": False,
        "plan_gate_reason": "",
        "plan_return_count": 0,
        "step_policies": {},
        "return_count": 0,
        "approval_history": [],
        "tool_cache": {},
        "tool_results": {},
        "source_excerpts": {},
        "support_review": {},
        "llm_calls": [],
        "events": [_event("task_started", "running", "任务已提交")],
    }


def planner_node(state: WorkflowState, registry: ToolRegistry) -> dict[str, Any]:
    try:
        plan, usage = _plan_agent(state["task"], registry, state.get("previous_feedback", ""))
    except Exception as exc:
        return _agent_failure(state, "planner", "plan", exc)
    reason = plan_approval_reason(plan, registry, state["task"])
    step_policies = {}
    for step in plan.steps:
        decision = policy_check(step, registry)
        step_policies[step.step_id] = {
            "risk_level": decision.risk_level,
            "requires_approval": registry.get(step.tool_name).requires_approval,
            "data_scope": decision.data_scope,
            "action": decision.action,
        }
    return {
        "plan": plan.model_dump(),
        "status": "awaiting_plan_approval" if reason else "running",
        "plan_approved": False,
        "plan_gate_reason": reason,
        "step_policies": step_policies,
        "support_review": {},
        "llm_calls": [*state.get("llm_calls", []), usage],
        "events": [_event(
            "plan_created", "completed", plan.summary,
            plan_gate_reason=reason,
        )],
    }


def _plan_agent(task: str, registry: ToolRegistry, feedback: str):
    if agents.mock_agents_enabled():
        return agents.mock_plan(task, registry, feedback)
    return agents.make_plan(task, registry, feedback)


def route_after_planner(state: WorkflowState) -> str:
    if state.get("status") in {"failed", "terminated"}:
        return "end"
    if state.get("plan_gate_reason"):
        return "plan_approval"
    return "data_agent"


def plan_approval_node(state: WorkflowState) -> dict[str, Any]:
    decision = interrupt({
        "kind": "plan_approval",
        "plan": state.get("plan", {}),
        "reason": state.get("plan_gate_reason", ""),
        "return_count": state.get("plan_return_count", 0),
        "max_returns": MAX_PLAN_RETURNS,
    })
    if not isinstance(decision, dict) or decision.get("decision") not in {"approve", "return"}:
        raise ValueError("计划审批决定无效")
    selected = decision["decision"]
    count = state.get("plan_return_count", 0) + (1 if selected == "return" else 0)
    terminated = selected == "return" and count >= MAX_PLAN_RETURNS
    comment = (decision.get("comment") or "").strip()
    history = [
        *state.get("approval_history", []),
        {
            "stage": "plan",
            "decision": selected,
            "comment": comment,
            "actor": "local_user",
            "at": datetime.now(timezone.utc).isoformat(),
        },
    ]
    return {
        "approval_decision": selected,
        "previous_feedback": comment,
        "plan_return_count": count,
        "plan_approved": selected == "approve",
        "approval_history": history,
        "status": "terminated" if terminated else "running",
        "events": [_event(
            "approval",
            "terminated" if terminated else "running",
            "计划退回次数达到上限，流程终止" if terminated else (
                "人工已批准计划" if selected == "approve" else "人工退回计划，重新规划"
            ),
            stage="plan",
            decision=selected,
            return_count=count,
        )],
    }


def route_after_plan_approval(state: WorkflowState) -> str:
    if state.get("status") == "terminated":
        return "end"
    if state.get("approval_decision") == "approve":
        return "data_agent"
    return "planner"


def data_agent_node(state: WorkflowState, registry: ToolRegistry) -> dict[str, Any]:
    try:
        results, cache, events = execute_plan(
            state["plan"],
            registry,
            state.get("tool_results", {}),
            state.get("tool_cache", {}),
            plan_approved=state.get("plan_approved", False),
        )
        return {
            "tool_results": results,
            "tool_cache": cache,
            "source_excerpts": _current_excerpts(state["plan"], results),
            "status": "running",
            "events": events,
        }
    except ExecutionHalt as exc:
        return {
            "status": "failed",
            "failed_node": "data_agent",
            "error_category": "tool_execution",
            "error": _safe_error_summary(exc),
            "tool_results": exc.results,
            "tool_cache": exc.cache,
            "source_excerpts": _current_excerpts(state["plan"], exc.results),
            "events": exc.events,
        }
    except Exception as exc:
        return {
            "status": "failed",
            "failed_node": "data_agent",
            "error_category": "provider_or_system",
            "error": _safe_error_summary(exc),
            "events": [_event("tool_failed", "failed", str(exc))],
        }


def route_after_data(state: WorkflowState) -> str:
    return "end" if state.get("status") == "failed" else "review_agent"


def _safe_error_summary(exc: Exception) -> str:
    import re

    message = str(exc) or type(exc).__name__
    message = re.sub(r"(?i)(api[_-]?key|authorization|bearer)\s*[:=]?\s*\S+", r"\1=[REDACTED]", message)
    message = re.sub(r"(?i)sk-[A-Za-z0-9_-]{8,}", "[REDACTED]", message)
    return message[:400]


def _agent_failure(state: WorkflowState, node: str, stage: str, exc: Exception) -> dict[str, Any]:
    if getattr(exc, "error_category", None):
        category = exc.error_category
    elif isinstance(exc, TimeoutError) or "timeout" in type(exc).__name__.lower() or "timed out" in str(exc).lower():
        category = "timeout"
    elif isinstance(exc, ValueError):
        category = "structured_output"
    else:
        category = "provider_or_system"
    usage = getattr(exc, "usage", None)
    llm_calls = [*state.get("llm_calls", [])]
    if isinstance(usage, dict):
        llm_calls.append(usage)
    message = _safe_error_summary(exc)
    return {
        "status": "failed",
        "failed_node": node,
        "error_category": category,
        "error": f"{stage}阶段失败（{category}）：{message}",
        "llm_calls": llm_calls,
        "events": [_event(
            "agent_failed", "failed", message,
            agent=node, stage=stage, error_category=category,
            attempts=(usage or {}).get("attempt_count", 1),
        )],
    }


def review_agent_node(state: WorkflowState) -> dict[str, Any]:
    try:
        if agents.mock_agents_enabled():
            review, usage = agents.mock_review(
                state["task"], state["plan"], state.get("tool_results", {}),
                state.get("source_excerpts", {}), state.get("previous_feedback", ""),
            )
        else:
            review, usage = agents.review_materials(
                state["task"], state["plan"], state.get("tool_results", {}),
                state.get("source_excerpts", {}), state.get("previous_feedback", ""),
            )
    except Exception as exc:
        return _agent_failure(state, "review_agent", "review", exc)
    review_dict = review.model_dump()
    return {
        "review": review_dict,
        "questions": review.questions,
        "status": "awaiting_human",
        "llm_calls": [*state.get("llm_calls", []), usage],
        "events": [_event("review_completed", "awaiting_human", "审核完成，等待人工确认")],
    }


def human_approval_node(state: WorkflowState) -> dict[str, Any]:
    decision = interrupt({
        "kind": "human_approval",
        "review": state.get("review", {}),
        "questions": state.get("questions", []),
        "return_count": state.get("return_count", 0),
        "max_returns": MAX_REVIEW_RETURNS,
    })
    if not isinstance(decision, dict) or decision.get("decision") not in {"approve", "return"}:
        raise ValueError("审批决定无效")
    selected = decision["decision"]
    count = state.get("return_count", 0) + (1 if selected == "return" else 0)
    terminated = selected == "return" and count >= MAX_REVIEW_RETURNS
    comment = (decision.get("comment") or "").strip()
    history = [
        *state.get("approval_history", []),
        {
            "stage": "report",
            "decision": selected,
            "comment": comment,
            "actor": "local_user",
            "at": datetime.now(timezone.utc).isoformat(),
        },
    ]
    status = "terminated" if terminated else "running"
    return {
        "approval_decision": selected,
        "previous_feedback": comment,
        "return_count": count,
        "approval_history": history,
        "status": status,
        "events": [_event(
            "approval", status,
            "退回次数达到上限，流程终止" if terminated else ("人工已确认" if selected == "approve" else "人工退回，重新规划"),
            stage="report",
            decision=selected,
            return_count=count,
        )],
    }


def route_after_approval(state: WorkflowState) -> str:
    if state.get("status") == "terminated":
        return "end"
    if state.get("approval_decision") == "approve":
        return "report_agent"
    return "planner"


def route_after_review(state: WorkflowState) -> str:
    return "end" if state.get("status") == "failed" else "human_approval"


def report_agent_node(state: WorkflowState) -> dict[str, Any]:
    try:
        if agents.mock_agents_enabled():
            report, usage = agents.mock_report(
                state["task"], state["review"], state.get("source_excerpts", {})
            )
        else:
            report, usage = agents.write_report(
                state["task"], state["review"], state.get("source_excerpts", {})
            )
    except Exception as exc:
        return _agent_failure(state, "report_agent", "report", exc)
    report_dict = report.model_dump()
    support_review = evaluate_report_support(report_dict, state.get("source_excerpts", {}))
    detail = "报告生成且来源 ID 校验通过"
    if support_review["needs_review"]:
        detail += "；部分声明支持度待人工确认"
    return {
        "report": report_dict,
        "support_review": support_review,
        "status": "succeeded",
        "llm_calls": [*state.get("llm_calls", []), usage],
        "events": [_event("report_completed", "succeeded", detail)],
    }


def run_workflow(graph, initial_state: dict[str, Any], config: dict[str, Any]) -> None:
    try:
        graph.invoke(initial_state, config=config)
    except Exception as exc:
        _mark_failed(graph, config, exc)


def resume_workflow(graph, config: dict[str, Any], decision: dict[str, str]) -> None:
    try:
        graph.invoke(Command(resume=decision), config=config)
    except Exception as exc:
        _mark_failed(graph, config, exc)


def _mark_failed(graph, config: dict[str, Any], exc: Exception) -> None:
    try:
        snapshot = graph.get_state(config)
        state = snapshot.values or {}
        graph.update_state(
            config,
            {
                "status": "failed",
                "error_category": "workflow_fallback",
                "error": _safe_error_summary(exc),
                "events": [_event("workflow_failed", "failed", _safe_error_summary(exc))],
            },
        )
    except Exception:
        return
