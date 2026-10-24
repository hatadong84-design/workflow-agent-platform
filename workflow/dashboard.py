from __future__ import annotations

import os
from datetime import datetime
from typing import Any

import httpx
import streamlit as st


API_URL = os.getenv("WORKFLOW_API_URL", "http://127.0.0.1:8001").rstrip("/")
SCENARIOS = {
    "供应商准入核验": {
        "icon": "🏢",
        "summary": "对照供应商登记记录与准入制度，整理有来源的核验结果和待确认事项。",
        "task": "查询供应商华星的登记记录，依据其类别查找供应商准入制度并读取相关条款，最后整理仍需人工核实的资料。",
        "input": "本地供应商清单 + 准入制度",
        "output": "登记事实、制度依据、待核实项",
        "boundary": "只提供核验材料；不批准供应商、不修改业务系统。",
    },
    "报销资料整理": {
        "icon": "🧾",
        "summary": "从本地制度中整理报销材料要求，并标出资料不足时需要确认的问题。",
        "task": "整理采购报销申请需要准备的资料，并列出材料不完整时需要向申请人确认的问题。",
        "input": "用户任务 + 本地报销制度",
        "output": "材料清单、来源依据、待确认问题",
        "boundary": "只读样例制度；不提交或审批报销。",
    },
    "安全事件整理": {
        "icon": "🛡️",
        "summary": "汇总安全事件资料，区分资料事实与需要负责人判断的事项。",
        "task": "整理发现可疑邮件后的安全事件处置要点，区分资料支持的事实和需要负责人确认的线索。",
        "input": "用户任务 + 本地安全处置资料",
        "output": "处置要点、引用来源、人工确认项",
        "boundary": "只整理材料；不执行隔离、删除或权限变更。",
    },
}
STATUS_LABELS = {
    "running": "处理中",
    "awaiting_plan_approval": "等待计划审批",
    "awaiting_human": "等待结果审核",
    "succeeded": "已完成",
    "failed": "执行失败",
    "terminated": "已终止",
}
TOOL_LABELS = {
    "query_supplier_registry": "查询供应商清单",
    "search_local_docs": "搜索本地制度",
    "read_local_doc": "读取来源摘录",
}
EVENT_LABELS = {
    "task_started": "任务已提交",
    "plan_created": "计划已生成",
    "policy_check": "工具策略检查",
    "tool_started": "开始执行工具",
    "tool_completed": "工具执行完成",
    "tool_cached": "复用已有结果",
    "tool_retry": "工具重试",
    "tool_skipped": "跳过步骤",
    "tool_failed": "工具执行失败",
    "review_completed": "资料审核完成",
    "approval": "人工审批",
    "report_completed": "报告已生成",
    "workflow_failed": "流程失败",
    "agent_failed": "Agent 执行失败",
}
FAILED_NODE_LABELS = {
    "planner": "Planner",
    "data_agent": "Data Agent 工具执行",
    "review_agent": "Review Agent",
    "report_agent": "Report Agent",
}


def _request(method: str, url: str, **kwargs) -> httpx.Response:
    kwargs.setdefault("trust_env", False)
    return httpx.request(method, url, **kwargs)


def _error(response: httpx.Response) -> str:
    try:
        body = response.json()
        return str(body.get("detail", body))
    except Exception:
        return response.text[:500]


def _get_run(run_id: str) -> dict[str, Any]:
    response = _request("GET", f"{API_URL}/runs/{run_id}", timeout=10)
    response.raise_for_status()
    return response.json()


def _icon(status: str) -> str:
    return {
        "done": "✅", "current": "🟡", "waiting": "⏳",
        "error": "❌", "pending": "○", "skipped": "↪️",
    }.get(status, "○")


def _phase_states(state: dict[str, Any]) -> list[tuple[str, str, str]]:
    status = state.get("status", "running")
    approval_stage = state.get("approval_stage")
    results = state.get("tool_results") or {}
    plan_exists = bool(state.get("plan"))
    review_exists = bool(state.get("review"))
    report_exists = bool(state.get("report"))
    failed = status == "failed"
    failed_node = state.get("failed_node")
    terminated = status == "terminated"

    phases = [
        ("提交任务", "done", "任务已进入工作流"),
        ("制定计划", "error" if failed_node == "planner" else "done" if plan_exists else ("current" if status == "running" else "pending"),
         "Planner 生成受约束的依赖计划"),
        ("查询资料",
         "done" if results and any(not item.get("_skipped") for item in results.values())
         else "error" if failed and failed_node in {None, "data_agent"}
         else "waiting" if approval_stage == "plan"
         else "current" if status == "running" and plan_exists
         else "pending",
         "只读工具按依赖顺序执行"),
        ("审核依据", "done" if review_exists else "error" if failed_node == "review_agent" or (failed and failed_node is None and results)
         else "current" if results and status == "running" else "pending",
         "Review 检查来源和资料缺口"),
        ("人工确认", "done" if report_exists else "error" if terminated
         else "current" if approval_stage == "report" else "pending",
         "审核人确认或退回重新规划"),
        ("生成报告", "done" if report_exists else "error" if failed_node == "report_agent" or (failed and failed_node is None) or terminated else "pending",
         "报告包含结论、来源和待核实项"),
    ]
    if failed and failed_node in {None, "data_agent"} and not review_exists and not results:
        phases[2] = ("查询资料", "error", "工具或系统执行失败")
    if terminated:
        phases = [
            (name, "skipped" if phase_status == "pending" else phase_status,
             "流程已终止" if phase_status == "pending" else desc)
            for name, phase_status, desc in phases
        ]
    return phases


def _render_flow(state: dict[str, Any]) -> None:
    st.markdown("#### 业务流程")
    phases = _phase_states(state)
    columns = st.columns(len(phases))
    for column, (label, phase_status, description) in zip(columns, phases):
        with column:
            st.markdown(f"### {_icon(phase_status)}")
            st.markdown(f"**{label}**")
            st.caption(description)


def _tool_status(step_id: str, result: dict[str, Any] | None,
                 events: list[dict[str, Any]], state: dict[str, Any]) -> str:
    if result is not None:
        return "skipped" if result.get("_skipped") else "done"
    if any(e.get("kind") == "tool_failed" and e.get("step_id") == step_id for e in events):
        return "error"
    if state.get("approval_stage") == "plan":
        return "waiting"
    if state.get("status") == "running":
        return "current"
    return "pending"


def _source_label(source_id: str) -> str:
    if source_id.startswith("supplier:"):
        return "供应商登记记录"
    if "#" in source_id:
        return source_id.rsplit("#", 1)[0].replace("_", " ")
    return source_id


def _render_citations(source_ids: list[str], sources: dict[str, str]) -> None:
    if not source_ids:
        st.caption("没有可引用来源")
        return
    for source_id in source_ids:
        excerpt = sources.get(source_id)
        if excerpt is None:
            st.error(f"来源不存在：{source_id}")
            continue
        with st.expander(f"📎 {_source_label(source_id)} · {source_id}"):
            st.write(excerpt)


def _render_plan(state: dict[str, Any]) -> None:
    plan = state.get("plan") or {}
    steps = plan.get("steps") or []
    if not steps:
        return
    st.markdown("#### Planner 计划")
    if plan.get("summary"):
        st.info(plan["summary"])

    results = state.get("tool_results") or {}
    events = state.get("events") or []
    policies = state.get("step_policies") or {}
    status_labels = {
        "done": "已完成", "waiting": "等待审批", "current": "执行中",
        "error": "失败", "skipped": "已跳过", "pending": "未开始",
    }

    for index, step in enumerate(steps, start=1):
        step_id = step.get("step_id", f"step-{index}")
        tool_name = step.get("tool_name", "")
        result = results.get(step_id)
        step_status = _tool_status(step_id, result, events, state)
        deps = step.get("depends_on") or []
        policy = policies.get(step_id, {})
        with st.container(border=True):
            heading, badge = st.columns([5, 2])
            heading.markdown(f"**{index}. {TOOL_LABELS.get(tool_name, tool_name)}**")
            badge.markdown(f"{_icon(step_status)} {status_labels[step_status]}")
            st.caption(
                f"步骤 {step_id}　·　依赖：{', '.join(deps) if deps else '无'}"
                f"　·　风险：{policy.get('risk_level', '—')}"
                f"　·　数据范围：{policy.get('data_scope', '—')}"
            )
            if policy.get("action") == "needs_approval":
                st.warning("此工具需要计划审批后才会执行。")
            if result is not None and not result.get("_skipped"):
                records = result.get("records")
                if isinstance(records, list) and records:
                    st.dataframe(
                        [
                            {
                                "供应商": row.get("name", ""),
                                "编号": row.get("supplier_id", ""),
                                "类别": row.get("category", ""),
                                "状态": row.get("status", ""),
                            }
                            for row in records
                        ],
                        width="stretch",
                        hide_index=True,
                    )
                elif result.get("matches"):
                    st.caption(f"找到 {len(result['matches'])} 条资料摘录。")
            elif result and result.get("_skipped"):
                st.caption("上游没有可用结果，本步骤跳过；后续结论会说明资料缺口。")
            with st.expander("步骤参数与原始结果"):
                st.json({"arguments": step.get("arguments", {}), "result": result})


def _render_events(events: list[dict[str, Any]]) -> None:
    for event in events:
        try:
            time_label = datetime.fromisoformat(event.get("at", "")).astimezone().strftime("%H:%M:%S")
        except (TypeError, ValueError):
            time_label = "—"
        label = EVENT_LABELS.get(event.get("kind", ""), event.get("kind", "事件"))
        with st.container(border=True):
            time_col, event_col = st.columns([1, 6])
            time_col.caption(time_label)
            event_col.markdown(f"**{label}**　{event.get('status', '—')}")
            if event.get("detail"):
                event_col.write(event["detail"])
            if event.get("step_id"):
                event_col.caption(f"步骤：{event['step_id']}")


def _render_review(state: dict[str, Any]) -> None:
    review = state.get("review") or {}
    if not review:
        return
    st.markdown("#### 审核结果")
    if review.get("summary"):
        st.write(review["summary"])
    sources = state.get("source_excerpts") or {}
    for finding in review.get("findings") or []:
        icon = "✅" if finding.get("supported") else "⚠️"
        with st.container(border=True):
            st.markdown(f"{icon} **{finding.get('statement', '审核项')}**")
            _render_citations(finding.get("source_ids") or [], sources)

    for heading, values in (
        ("资料限制", review.get("limitations") or []),
        ("待人工确认", review.get("questions") or state.get("questions") or []),
    ):
        if values:
            st.markdown(f"**{heading}**")
            for value in values:
                st.markdown(f"- {value}")


def _render_report(state: dict[str, Any]) -> None:
    report = state.get("report") or {}
    if not report:
        return
    sources = state.get("source_excerpts") or {}
    st.markdown("#### 核验报告")
    st.markdown(f"### {report.get('title', '流程报告')}")
    if report.get("summary"):
        st.info(report["summary"])
        _render_citations(report.get("summary_source_ids") or [], sources)
    claims = report.get("claims") or []
    if claims:
        st.markdown("**有来源支持的事项**")
        for claim in claims:
            with st.container(border=True):
                st.write(claim.get("text", ""))
                _render_citations(claim.get("source_ids") or [], sources)
    limitations = report.get("limitations") or []
    if limitations:
        st.markdown("**需要补充或人工核实**")
        for item in limitations:
            st.warning(item)

    support = state.get("support_review") or {}
    if support:
        with st.expander("引用支持度检查"):
            st.caption("词项覆盖率是启发式提示，不等同于语义蕴含判断。")
            st.json(support)


def _render_approval(run_id: str, state: dict[str, Any]) -> None:
    status = state.get("status")
    stage = state.get("approval_stage")
    if status not in {"awaiting_plan_approval", "awaiting_human"}:
        return

    is_plan = stage == "plan"
    st.markdown("#### " + ("计划审批：执行前确认" if is_plan else "结果审核：确认后生成报告"))
    if is_plan:
        st.warning(state.get("plan_gate_reason") or "此计划需要人工批准。")
        st.info("批准前不会调用这些工具；批准后仅执行计划中的只读工具。")
        prompt = "批准执行此计划？"
        comment_label = "计划修改意见"
        approve_label = "批准计划"
    else:
        st.info("查看来源和审核问题后，确认生成报告，或退回要求重新规划。")
        prompt = "确认当前审核结果？"
        comment_label = "退回说明"
        approve_label = "确认并生成报告"

    with st.form(f"approval_{stage}_{run_id}"):
        decision = st.radio(
            prompt,
            ["approve", "return"],
            format_func=lambda value: approve_label if value == "approve" else "退回并重新规划",
            horizontal=True,
            key=f"decision_{stage}_{run_id}",
        )
        comment = st.text_area(comment_label, max_chars=2000, key=f"comment_{stage}_{run_id}")
        submitted = st.form_submit_button("提交决定", type="primary")
    if submitted:
        try:
            response = _request(
                "POST", f"{API_URL}/runs/{run_id}/approval",
                json={"decision": decision, "comment": comment}, timeout=15,
            )
            response.raise_for_status()
            st.success("审批已提交，流程继续运行。")
            st.rerun()
        except Exception as exc:
            if isinstance(exc, httpx.HTTPStatusError):
                st.error(_error(exc.response))
            else:
                st.error(f"提交失败：{exc}")


def _render_metrics(run_id: str) -> None:
    try:
        response = _request("GET", f"{API_URL}/runs/{run_id}/metrics", timeout=10)
        response.raise_for_status()
        values = response.json()
        audit_response = _request("GET", f"{API_URL}/runs/{run_id}/audit/verify", timeout=10)
        audit_response.raise_for_status()
        audit = audit_response.json()
    except Exception as exc:
        st.warning(f"运行指标暂不可用：{exc}")
        return

    usage = values.get("token_usage") or {}
    columns = st.columns(5)
    columns[0].metric("工具调用", values.get("tool_calls", 0))
    columns[1].metric("结果复用", values.get("tool_cached", 0))
    columns[2].metric("重试", values.get("retries", 0))
    columns[3].metric("总 Token", usage.get("total_tokens", "—") if usage else "—")
    columns[4].metric("轨迹校验", "通过" if audit.get("valid") else "未通过")
    elapsed = values.get("event_span_ms")
    st.caption(f"事件跨度：{elapsed} ms" if elapsed is not None else "事件跨度：暂无")
    with st.expander("完整运行指标"):
        st.json(values)


def _render_exports(run_id: str) -> None:
    if st.session_state.get("active_status") != "succeeded":
        return
    try:
        markdown = _request("GET", f"{API_URL}/runs/{run_id}/export", params={"format": "md"}, timeout=15)
        payload = _request("GET", f"{API_URL}/runs/{run_id}/export", params={"format": "json"}, timeout=15)
        markdown.raise_for_status()
        payload.raise_for_status()
    except Exception as exc:
        st.warning(f"审计包暂不可用：{exc}")
        return
    left, right = st.columns(2)
    left.download_button(
        "下载 Markdown 审计包", data=markdown.content,
        file_name=f"workflow_{run_id}.md", mime="text/markdown",
        width="stretch",
    )
    right.download_button(
        "下载 JSON 执行轨迹", data=payload.content,
        file_name=f"workflow_{run_id}.json", mime="application/json",
        width="stretch",
    )


@st.fragment(run_every="2s")
def render_run(run_id: str) -> None:
    try:
        state = _get_run(run_id)
    except Exception as exc:
        st.error(f"无法查询任务：{exc}")
        if st.button("返回场景选择"):
            st.session_state.pop("active_run_id", None)
            if "run_id" in st.query_params:
                del st.query_params["run_id"]
            st.rerun()
        return

    status = state.get("status", "running")
    st.session_state["active_status"] = status
    scenario = st.session_state.get("active_scenario")
    if not scenario:
        task = state.get("task", "")
        scenario = next(
            (name for name, preset in SCENARIOS.items() if preset["task"] == task),
            "自定义工作流任务",
        )
    st.markdown(f"## {scenario}")
    st.caption(f"任务编号：{run_id}　·　当前状态：**{STATUS_LABELS.get(status, status)}**")
    _render_flow(state)

    left, right = st.columns([5, 1])
    with left:
        if status == "running":
            st.info("流程正在运行，页面每 2 秒自动刷新。")
        elif status == "awaiting_plan_approval":
            st.info("流程在执行前等待计划批准，工具尚未运行。")
        elif status == "awaiting_human":
            st.info("资料审核完成，等待人工确认或退回。")
        elif status == "succeeded":
            st.success("报告已生成，可回看来源并下载审计包。")
        elif status == "failed":
            failed_node = FAILED_NODE_LABELS.get(state.get("failed_node"), "流程")
            category = state.get("error_category", "未分类错误")
            st.error(f"{failed_node}阶段失败（{category}）；可查看失败事件和调用记录。")
        elif status == "terminated":
            st.warning("退回次数达到上限，流程已终止。")
    with right:
        if st.button("新建任务", width="stretch", key=f"new_{run_id}"):
            st.session_state.pop("active_run_id", None)
            st.session_state.pop("active_status", None)
            if "run_id" in st.query_params:
                del st.query_params["run_id"]
            st.rerun()

    if state.get("error"):
        st.error(state["error"])
    _render_plan(state)
    _render_approval(run_id, state)

    sources = state.get("source_excerpts") or {}
    if sources:
        with st.expander(f"查看全部依据（{len(sources)} 个来源）"):
            for source_id, excerpt in sources.items():
                st.markdown(f"**{_source_label(source_id)}** · {source_id}")
                st.write(excerpt)
                st.divider()

    _render_review(state)
    _render_report(state)

    events = state.get("events") or []
    if events:
        with st.expander(f"查看执行时间线（{len(events)} 条事件）", expanded=status in {"failed", "terminated"}):
            _render_events(events)

    if status in {"succeeded", "failed", "terminated"}:
        st.markdown("#### 本次运行数据")
        _render_metrics(run_id)
    if status == "succeeded":
        with st.expander("导出本次运行审计包"):
            _render_exports(run_id)
    with st.expander("调试信息（原始工作流状态）"):
        st.json(state)


def main() -> None:
    st.set_page_config(page_title="供应商准入核验工作台", page_icon="🧭", layout="wide")
    st.markdown(
        """
        <style>
        .block-container {max-width: 1180px; padding-top: 2.2rem; padding-bottom: 3rem;}
        div[data-testid="stMetric"] {background: #f7f9fc; border: 1px solid #e7ebf2; padding: .75rem 1rem; border-radius: 12px;}
        </style>
        """,
        unsafe_allow_html=True,
    )
    st.title("企业流程自动化 · 供应商准入核验")
    st.write("把供应商登记信息与准入制度资料放进同一条可追踪流程，输出有来源的核验材料和待确认事项。")
    st.caption("演示数据来自本地 CSV 与样例制度；系统不会自动批准供应商，也不会写入企业业务系统。")

    run_id_from_url = st.query_params.get("run_id")
    if run_id_from_url and not st.session_state.get("active_run_id"):
        st.session_state["active_run_id"] = run_id_from_url
    active_run_id = st.session_state.get("active_run_id")
    if active_run_id:
        render_run(active_run_id)
        return

    col1, col2, col3 = st.columns(3)
    with col1, st.container(border=True):
        st.markdown("**输入**")
        st.write("供应商记录与自然语言核验任务")
    with col2, st.container(border=True):
        st.markdown("**处理**")
        st.write("Planner 规划 → 只读工具查证 → Review 审核")
    with col3, st.container(border=True):
        st.markdown("**输出**")
        st.write("制度引用、登记事实与人工确认项")

    st.markdown("### 选择一个演示场景")
    selected = st.radio("演示场景", list(SCENARIOS), horizontal=True, label_visibility="collapsed")
    if st.session_state.get("_last_scenario") != selected:
        st.session_state["task_editor"] = SCENARIOS[selected]["task"]
        st.session_state["_last_scenario"] = selected
    scenario = SCENARIOS[selected]

    with st.container(border=True):
        st.markdown(f"### {scenario['icon']} {selected}")
        st.write(scenario["summary"])
        info1, info2, info3 = st.columns(3)
        info1.caption(f"**输入** · {scenario['input']}")
        info2.caption(f"**预期产出** · {scenario['output']}")
        info3.caption(f"**边界** · {scenario['boundary']}")

    task = st.text_area(
        "任务描述（可以直接使用预设内容）",
        max_chars=4000,
        height=100,
        key="task_editor",
    )
    if st.button("开始核验流程", type="primary", width="stretch"):
        if not task.strip():
            st.error("请先填写任务描述。")
        else:
            try:
                response = _request(
                    "POST", f"{API_URL}/runs",
                    json={"task": task.strip()}, timeout=15,
                )
                response.raise_for_status()
                st.session_state["active_run_id"] = response.json()["run_id"]
                st.query_params["run_id"] = response.json()["run_id"]
                st.session_state["active_scenario"] = selected
                st.session_state["active_status"] = "running"
                st.rerun()
            except Exception as exc:
                if isinstance(exc, httpx.HTTPStatusError):
                    st.error(f"任务提交失败：{_error(exc.response)}")
                else:
                    st.error(f"任务提交失败：{exc}")
                st.info("确认 FastAPI 已在本机 8001 端口启动；启动命令见下方项目说明。")

    try:
        response = _request("GET", f"{API_URL}/tools", timeout=5)
        response.raise_for_status()
        with st.expander("查看可用工具与策略"):
            st.json(response.json().get("tools", []))
    except Exception:
        with st.expander("工作流服务未连接"):
            st.write(f"启动 FastAPI 后端，再从本页提交任务。当前 API 地址：{API_URL}")


if __name__ == "__main__":
    main()
