from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from .audit import verify_audit_chain
from .metrics import compute_metrics


SCHEMA_VERSION = "1.0"


def build_export(state: dict) -> dict[str, Any]:
    events = list(state.get("events", []))
    return {
        "schema_version": SCHEMA_VERSION,
        "run": {
            "run_id": state.get("run_id", ""),
            "task": state.get("task", ""),
            "status": state.get("status", ""),
            "error": state.get("error", ""),
        },
        "plan": state.get("plan", {}),
        "step_policies": state.get("step_policies", {}),
        "tool_results": state.get("tool_results", {}),
        "source_excerpts": state.get("source_excerpts", {}),
        "review": state.get("review", {}),
        "report": state.get("report", {}),
        "support_review": state.get("support_review", {}),
        "approval_history": state.get("approval_history", []),
        "questions": state.get("questions", []),
        "llm_calls": state.get("llm_calls", []),
        "events": events,
        "metrics": compute_metrics(state),
        "audit": verify_audit_chain(events),
    }


def _source_label(source_ids: list[str]) -> str:
    return "、".join(source_ids) if source_ids else "无引用"


def _render_support(trace: dict[str, Any]) -> list[str]:
    support = trace.get("support_review") or {}
    lines = [
        "## 引用支持度",
        "",
        f"确定性 token 覆盖率阈值：{support.get('threshold', 0.5)}。"
        "引用合法不等于语义支持，弱支持项需人工复核。",
        "",
    ]
    entries = []
    if support.get("summary"):
        entries.append(("摘要", support["summary"]))
    entries.extend(("声明", item) for item in support.get("claims", []))
    if not entries:
        lines.append("无报告声明可检查。")
        lines.append("")
        return lines
    for kind, item in entries:
        lines.append(
            f"- {kind}：{item.get('status', '')}，覆盖率 {item.get('coverage', 0.0)}，"
            f"来源 {_source_label(item.get('source_ids', []))}"
        )
        lines.append(f"  - 文本：{item.get('text', '')}")
    if support.get("needs_review"):
        lines.extend([
            "",
            "> 引用已核验、支持度待人工确认。",
        ])
    lines.append("")
    return lines


def render_markdown(trace: dict[str, Any]) -> str:
    run = trace.get("run", {})
    report = trace.get("report") or {}
    plan = trace.get("plan") or {}
    results = trace.get("tool_results") or {}
    step_policies = trace.get("step_policies") or {}
    metrics = trace.get("metrics") or {}
    audit = trace.get("audit") or {}
    lines = [
        "# 企业流程自动化审计包",
        "",
        f"- run_id：{run.get('run_id', '')}",
        f"- 任务：{run.get('task', '')}",
        f"- 最终状态：{run.get('status', '')}",
        f"- 生成时间：{datetime.now(timezone.utc).isoformat()}",
    ]
    if run.get("error"):
        lines.append(f"- 错误：{run['error']}")
    lines.extend(["", "## 最终报告", ""])
    if report:
        lines.extend([
            f"### {report.get('title', '')}",
            "",
            report.get("summary", ""),
            "",
        ])
        for index, claim in enumerate(report.get("claims", []), start=1):
            lines.append(
                f"{index}. {claim.get('text', '')}（来源：{_source_label(claim.get('source_ids', []))}）"
            )
        if report.get("limitations"):
            lines.extend(["", "**限制**", ""])
            lines.extend(f"- {item}" for item in report["limitations"])
    else:
        lines.append("尚未生成报告。")
    lines.extend(["", *_render_support(trace)])

    lines.extend(["## 来源摘录", ""])
    excerpts = trace.get("source_excerpts") or {}
    if excerpts:
        for source_id, excerpt in excerpts.items():
            lines.extend([f"### {source_id}", "", f"> {excerpt}", ""])
    else:
        lines.extend(["无来源摘录。", ""])

    lines.extend(["## 计划与执行", ""])
    steps = plan.get("steps", [])
    if steps:
        lines.extend(["| 步骤 | 工具 | 风险 | 依赖 | 状态 |", "| --- | --- | --- | --- | --- |"])
        for step in steps:
            step_id = step.get("step_id", "")
            result = results.get(step_id, {})
            if result.get("_skipped"):
                status = "跳过"
            elif step_id in results:
                status = "完成"
            else:
                status = "未执行"
            policy = step_policies.get(step_id) or {}
            lines.append(
                f"| {step_id} | {step.get('tool_name', '')} | "
                f"{policy.get('risk_level', '未声明')} | "
                f"{', '.join(step.get('depends_on', [])) or '-'} | {status} |"
            )
        lines.extend(["", "步骤参数："])
        for step in steps:
            lines.append(f"- {step.get('step_id')}：`{json.dumps(step.get('arguments', {}), ensure_ascii=False)}`")
    else:
        lines.append("无计划。")
    lines.append("")

    lines.extend(["## 审批历史", ""])
    history = trace.get("approval_history") or []
    if history:
        for stage, label in (("plan", "计划门"), ("report", "报告门")):
            entries = [entry for entry in history if entry.get("stage", "report") == stage]
            lines.append(f"### {label}")
            lines.append("")
            if not entries:
                lines.append("无记录。")
                lines.append("")
                continue
            for entry in entries:
                lines.append(
                    f"- {entry.get('decision', '')}（{entry.get('actor', '')}，"
                    f"{entry.get('at', '')}）：{entry.get('comment', '') or '无意见'}"
                )
            lines.append("")
    else:
        lines.extend(["无审批记录。", ""])

    lines.extend(["## 待确认问题", ""])
    questions = trace.get("questions") or []
    lines.extend(f"- {question}" for question in questions) if questions else lines.append("无。")
    lines.append("")

    lines.extend(["## 运行指标", ""])
    lines.extend(["| 指标 | 值 |", "| --- | --- |"])
    for key in (
        "tool_calls", "tool_completed", "tool_cached", "tool_failed", "tool_skipped",
        "retries", "cache_hit_rate", "policy_denials", "plan_gate_visits",
        "report_gate_visits", "plan_returns", "report_returns", "llm_calls",
        "token_usage", "node_durations_ms", "event_span_ms", "audit_valid",
    ):
        value = metrics.get(key)
        lines.append(f"| {key} | {json.dumps(value, ensure_ascii=False) if isinstance(value, (dict, list)) else value} |")
    lines.extend(["", "## 审计链", ""])
    lines.extend([
        f"- 校验结果：{'通过' if audit.get('valid') else '失败'}",
        f"- 事件数：{audit.get('events_checked', 0)}",
        f"- 根哈希：{audit.get('root_hash', '')}",
    ])
    if audit.get("reason"):
        lines.append(f"- 失败原因：{audit['reason']}")
    lines.append("")
    return "\n".join(lines)
