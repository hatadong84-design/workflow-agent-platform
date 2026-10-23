from __future__ import annotations

import os

import httpx
import streamlit as st


API_URL = os.getenv("WORKFLOW_API_URL", "http://127.0.0.1:8001").rstrip("/")
DEFAULT_TASK = "查询供应商华星的登记记录，依据其类别查找供应商准入制度并读取相关条款，最后整理仍需人工核实的资料。"


def _request(method: str, url: str, **kwargs) -> httpx.Response:
    # 本机 API 不走系统代理：Windows 代理设置会把 127.0.0.1 也转发出去并返回 503
    kwargs.setdefault("trust_env", False)
    return httpx.request(method, url, **kwargs)


def _error(response: httpx.Response) -> str:
    try:
        return str(response.json().get("detail", response.json()))
    except Exception:
        return response.text[:500]


def _get_run(run_id: str) -> dict:
    response = _request("GET", f"{API_URL}/runs/{run_id}", timeout=10)
    response.raise_for_status()
    return response.json()


def render_run(run_id: str) -> None:
    try:
        state = _get_run(run_id)
    except Exception as exc:
        st.error(f"无法查询任务：{exc}")
        return
    status = state.get("status", "running")
    st.subheader("任务执行状态")
    st.write(f"任务 ID：`{run_id}`　状态：**{status}**")
    left, right = st.columns(2)
    with left:
        if st.button("刷新状态", use_container_width=True):
            st.rerun()
    with right:
        if st.button("新建任务", use_container_width=True):
            st.session_state.pop("active_run_id", None)
            st.rerun()

    approval_stage = state.get("approval_stage")
    if approval_stage:
        st.caption(f"当前审批阶段：{'计划门' if approval_stage == 'plan' else '报告门'}")
    if state.get("error"):
        st.error(state["error"])
    if state.get("plan"):
        st.markdown("#### Planner 计划")
        st.json(state["plan"])
        if state.get("step_policies"):
            st.markdown("#### 工具策略")
            st.dataframe(
                [
                    {"step_id": step_id, **policy}
                    for step_id, policy in state["step_policies"].items()
                ],
                use_container_width=True,
                hide_index=True,
            )
    if state.get("events"):
        st.markdown("#### 只追加执行事件")
        st.dataframe(state["events"], use_container_width=True, hide_index=True)
    if state.get("tool_results"):
        st.markdown("#### Data Agent 工具结果")
        st.json(state["tool_results"])
    if state.get("source_excerpts"):
        st.markdown("#### 来源摘录")
        for source_id, excerpt in state["source_excerpts"].items():
            with st.expander(source_id):
                st.write(excerpt)
    if state.get("review"):
        st.markdown("#### Review Agent")
        st.json(state["review"])
    if state.get("questions"):
        st.markdown("#### 人工确认问题")
        for question in state["questions"]:
            st.markdown(f"- {question}")
    if state.get("approval_history"):
        with st.expander("审批记录"):
            st.json(state["approval_history"])

    if status == "awaiting_plan_approval":
        st.markdown("#### 计划门：执行前审批")
        st.warning(f"策略触发原因：{state.get('plan_gate_reason', '')}")
        st.info("批准后才会执行工具；退回会交由 Planner 重新规划，尚未产生工具结果。")
        decision = st.radio(
            "计划决定",
            ["approve", "return"],
            format_func=lambda value: "批准计划并执行" if value == "approve" else "退回并重新规划",
            horizontal=True,
            key="plan_decision",
        )
        comment = st.text_area("计划意见", max_chars=2000, key="plan_comment")
        if st.button("提交计划决定", type="primary"):
            try:
                response = _request("POST", 
                    f"{API_URL}/runs/{run_id}/approval",
                    json={"decision": decision, "comment": comment},
                    timeout=15,
                )
                if response.status_code >= 400:
                    st.error(_error(response))
                else:
                    st.success("计划决定已提交")
                    st.rerun()
            except Exception as exc:
                st.error(f"提交失败：{exc}")

    if status == "awaiting_human":
        st.markdown("#### 人工审核")
        st.info("确认后生成报告；退回会交由 Planner 根据意见重新规划，成功工具结果会按工具名和参数复用。")
        decision = st.radio(
            "决定",
            ["approve", "return"],
            format_func=lambda value: "确认并生成报告" if value == "approve" else "退回并重新规划",
            horizontal=True,
        )
        comment = st.text_area("审核意见", max_chars=2000)
        if st.button("提交审核决定", type="primary"):
            try:
                response = _request("POST", 
                    f"{API_URL}/runs/{run_id}/approval",
                    json={"decision": decision, "comment": comment},
                    timeout=15,
                )
                if response.status_code >= 400:
                    st.error(_error(response))
                else:
                    st.success("审核决定已提交")
                    st.rerun()
            except Exception as exc:
                st.error(f"提交失败：{exc}")

    support = state.get("support_review") or {}
    if support:
        with st.expander("引用支持度检查", expanded=support.get("needs_review", False)):
            st.caption(
                "token 覆盖率启发式检查：引用合法不等于语义支持，weak 项需人工复核。"
            )
            entries = []
            if support.get("summary"):
                entries.append({"类型": "摘要", **support["summary"]})
            entries.extend({"类型": "声明", **claim} for claim in support.get("claims", []))
            if entries:
                st.dataframe(
                    [
                        {
                            "类型": item["类型"],
                            "状态": item.get("status"),
                            "覆盖率": item.get("coverage"),
                            "来源": ", ".join(item.get("source_ids", [])),
                            "文本": item.get("text", ""),
                        }
                        for item in entries
                    ],
                    use_container_width=True,
                    hide_index=True,
                )
            if support.get("needs_review"):
                st.warning("引用已核验、支持度待人工确认。")

    if state.get("report"):
        report = state["report"]
        st.markdown("### 最终报告")
        st.markdown(f"#### {report.get('title', '资料整理报告')}")
        st.write(report.get("summary", ""))
        st.caption("摘要来源：" + ", ".join(report.get("summary_source_ids", [])))
        for claim in report.get("claims", []):
            st.markdown(f"- {claim.get('text', '')}")
            st.caption("来源：" + ", ".join(claim.get("source_ids", [])))
        if report.get("limitations"):
            st.markdown("#### 待核实与限制")
            for limitation in report["limitations"]:
                st.markdown(f"- {limitation}")
    if status in {"succeeded", "failed", "terminated"}:
        try:
            metrics = _request("GET", f"{API_URL}/runs/{run_id}/metrics", timeout=10).json()
            audit = _request("GET", f"{API_URL}/runs/{run_id}/audit/verify", timeout=10).json()
            st.markdown("#### 运行指标")
            columns = st.columns(4)
            columns[0].metric("工具调用", metrics.get("tool_calls", 0))
            columns[1].metric("缓存命中率", metrics.get("cache_hit_rate", 0.0))
            columns[2].metric("重试次数", metrics.get("retries", 0))
            columns[3].metric("审计链", "通过" if audit.get("valid") else "失败")
            with st.expander("完整指标", expanded=False):
                st.json(metrics)
        except Exception as exc:
            st.warning(f"暂时无法读取运行指标：{exc}")

    if status == "succeeded":
        st.markdown("#### 审计包导出")
        try:
            markdown = _request("GET", 
                f"{API_URL}/runs/{run_id}/export", params={"format": "md"}, timeout=15
            )
            payload = _request("GET", 
                f"{API_URL}/runs/{run_id}/export", params={"format": "json"}, timeout=15
            )
            left, right = st.columns(2)
            with left:
                st.download_button(
                    "下载 Markdown 审计包",
                    data=markdown.content,
                    file_name=f"workflow_{run_id}.md",
                    mime="text/markdown",
                    use_container_width=True,
                )
            with right:
                st.download_button(
                    "下载 JSON 轨迹",
                    data=payload.content,
                    file_name=f"workflow_{run_id}.json",
                    mime="application/json",
                    use_container_width=True,
                )
        except Exception as exc:
            st.warning(f"暂时无法生成审计包：{exc}")

    if state.get("llm_calls"):
        with st.expander("模型调用耗时与 token"):
            st.dataframe(state["llm_calls"], use_container_width=True, hide_index=True)


def main() -> None:
    st.set_page_config(page_title="企业流程自动化平台", layout="wide")
    st.title("企业流程自动化多 Agent 平台")
    st.caption("Planner 规划 · Data Agent 执行只读工具 · Review Agent 审核 · Report Agent 汇总")

    active_run_id = st.session_state.get("active_run_id")
    if active_run_id:
        render_run(active_run_id)
        return

    try:
        response = _request("GET", f"{API_URL}/tools", timeout=5)
        response.raise_for_status()
        tools = response.json().get("tools", [])
        with st.expander("已注册只读、幂等工具"):
            st.json(tools)
    except Exception as exc:
        st.warning(f"暂时无法读取工具目录：{exc}")

    task = st.text_area("自然语言任务", value=DEFAULT_TASK, max_chars=4000)
    if st.button("提交工作流", type="primary"):
        try:
            response = _request("POST", f"{API_URL}/runs", json={"task": task}, timeout=15)
            if response.status_code >= 400:
                st.error(_error(response))
            else:
                st.session_state.active_run_id = response.json()["run_id"]
                st.rerun()
        except Exception as exc:
            st.error(f"提交失败：{exc}")


if __name__ == "__main__":
    main()
