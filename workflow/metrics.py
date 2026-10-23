from __future__ import annotations

from datetime import datetime
from typing import Any

from .audit import verify_audit_chain


TOOL_EVENT_KINDS = {
    "tool_started",
    "tool_cached",
    "tool_completed",
    "tool_failed",
    "tool_retry",
    "tool_skipped",
    "policy_check",
}
ROLE_NODES = {
    "Planner": "planner",
    "Review Agent": "review",
    "Report Agent": "report",
}


def _count(events: list[dict], kind: str, status: str | None = None) -> int:
    return sum(
        1
        for event in events
        if event.get("kind") == kind and (status is None or event.get("status") == status)
    )


def _parse_at(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def _span_ms(events: list[dict]) -> float:
    timestamps = [parsed for event in events if (parsed := _parse_at(event.get("at")))]
    if len(timestamps) < 2:
        return 0.0
    return round((max(timestamps) - min(timestamps)).total_seconds() * 1000, 1)


def _token_totals(calls: list[dict]) -> dict[str, int] | None:
    keys = ("input_tokens", "output_tokens", "total_tokens")
    if not any(isinstance(call.get(key), (int, float)) for call in calls for key in keys):
        return None
    totals = {key: 0 for key in keys}
    for call in calls:
        for key in keys:
            value = call.get(key)
            if isinstance(value, (int, float)):
                totals[key] += int(value)
    return totals


def compute_metrics(state: dict) -> dict[str, Any]:
    events = list(state.get("events", []))
    calls = list(state.get("llm_calls", []))
    history = list(state.get("approval_history", []))

    started = _count(events, "tool_started")
    cached = _count(events, "tool_cached")
    completed = _count(events, "tool_completed")
    cache_denominator = cached + completed
    cache_hit_rate = round(cached / cache_denominator, 4) if cache_denominator else 0.0

    durations = {"planner": 0.0, "data_agent": 0.0, "review": 0.0, "report": 0.0, "total": 0.0}
    for call in calls:
        node = ROLE_NODES.get(str(call.get("role", "")))
        if node:
            durations[node] += float(call.get("elapsed_ms") or 0)
    tool_events = [event for event in events if event.get("kind") in TOOL_EVENT_KINDS]
    durations["data_agent"] = _span_ms(tool_events)
    durations["total"] = _span_ms(events)
    durations = {key: round(value, 1) for key, value in durations.items()}

    audit = verify_audit_chain(events)
    return {
        "run_id": state.get("run_id", ""),
        "status": state.get("status", ""),
        "event_count": len(events),
        "tool_calls": started,
        "tool_completed": completed,
        "tool_cached": cached,
        "tool_failed": _count(events, "tool_failed"),
        "tool_skipped": _count(events, "tool_skipped"),
        "retries": _count(events, "tool_retry"),
        "cache_hit_rate": cache_hit_rate,
        "policy_denials": _count(events, "policy_check", "deny"),
        "plan_gate_visits": sum(1 for entry in history if entry.get("stage") == "plan"),
        "report_gate_visits": sum(
            1 for entry in history if entry.get("stage", "report") == "report"
        ),
        "plan_returns": int(state.get("plan_return_count", 0) or 0),
        "report_returns": int(state.get("return_count", 0) or 0),
        "llm_calls": len(calls),
        "token_usage": _token_totals(calls),
        "node_durations_ms": durations,
        "event_span_ms": durations["total"],
        "audit_valid": audit["valid"],
    }
