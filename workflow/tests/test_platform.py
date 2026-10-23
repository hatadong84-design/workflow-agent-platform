from __future__ import annotations

import os
import time
import unittest
from unittest.mock import patch

from pydantic import BaseModel, Field, ValidationError
from langgraph.types import Command

from workflow import agents
from workflow.audit import chain_events, verify_audit_chain
from workflow.export import build_export, render_markdown
from workflow.graph import (
    ApprovalRequiredError,
    PolicyDeniedError,
    ToolExecutionError,
    build_graph,
    execute_plan,
)
from workflow.metrics import compute_metrics
from workflow.retrieval import DOCS_DIR, _safe_doc_path, read_document, source_excerpts
from workflow.schemas import PlannedToolStep, WorkflowPlan
from workflow.support import evaluate_claim, evaluate_report_support
from workflow.suppliers import (
    MAX_ROW_EXCERPT_CHARS,
    MAX_RETURNED_RECORDS,
    SupplierQueryInput,
    query_supplier_registry,
)
from workflow.tool_registry import (
    ReadInput,
    SearchInput,
    ToolRegistry,
    ToolSpec,
    default_registry,
    plan_approval_reason,
    policy_check,
)


LOW_POLICY = {"risk_level": "low", "requires_approval": False, "data_scope": "internal"}


class EchoInput(BaseModel):
    value: str = Field(min_length=1)


class PlatformUnitTests(unittest.TestCase):
    def test_plan_rejects_cycles_and_unknown_dependencies(self):
        with self.assertRaises(ValidationError):
            WorkflowPlan(
                summary="cycle",
                steps=[
                    PlannedToolStep(step_id="a", tool_name="x", depends_on=["b"]),
                    PlannedToolStep(step_id="b", tool_name="x", depends_on=["a"]),
                ],
            )
        with self.assertRaises(ValidationError):
            WorkflowPlan(
                summary="missing dependency",
                steps=[PlannedToolStep(step_id="a", tool_name="x", depends_on=["missing"])],
            )
        six_steps = WorkflowPlan(
            summary="six steps",
            steps=[PlannedToolStep(step_id=f"s{index}", tool_name="x") for index in range(6)],
        )
        self.assertEqual(len(six_steps.steps), 6)
        with self.assertRaises(ValidationError):
            WorkflowPlan(
                summary="too many steps",
                steps=[PlannedToolStep(step_id=f"s{index}", tool_name="x") for index in range(7)],
            )

    def test_plan_rejects_unknown_tools_invalid_arguments_and_unlisted_result_refs(self):
        registry = default_registry()
        with self.assertRaisesRegex(ValueError, "未注册工具"):
            agents._validate_plan(
                WorkflowPlan(
                    summary="unknown tool",
                    steps=[PlannedToolStep(step_id="a", tool_name="delete_records")],
                ),
                registry,
            )
        with self.assertRaises(ValidationError):
            agents._validate_plan(
                WorkflowPlan(
                    summary="bad args",
                    steps=[PlannedToolStep(
                        step_id="a", tool_name="search_local_docs",
                        arguments={"query": "search", "unexpected": True},
                    )],
                ),
                registry,
            )
        with self.assertRaisesRegex(ValueError, "未声明依赖"):
            agents._validate_plan(
                WorkflowPlan(
                    summary="undeclared ref",
                    steps=[
                        PlannedToolStep(step_id="a", tool_name="search_local_docs", arguments={"query": "x"}),
                        PlannedToolStep(
                            step_id="b", tool_name="read_local_doc",
                            arguments={"source_id": {"$ref": "a.matches.0.source_id"}},
                            depends_on=[],
                        ),
                    ],
                ),
                registry,
            )

    def test_registry_rejects_non_readonly_and_retries_idempotent_tool(self):
        registry = ToolRegistry()
        with self.assertRaises(ValueError):
            registry.register(ToolSpec("write", "write", EchoInput, lambda value: {}, read_only=False))

        calls = 0

        def flaky(value: str):
            nonlocal calls
            calls += 1
            if calls == 1:
                raise RuntimeError("temporary read failure")
            return {"value": value}

        registry.register(ToolSpec("read", "read", EchoInput, flaky, max_attempts=2, **LOW_POLICY))
        retries = []
        result = registry.invoke("read", {"value": "ok"}, on_retry=lambda attempt, error: retries.append(attempt))
        self.assertEqual(result, {"value": "ok"})
        self.assertEqual(calls, 2)
        self.assertEqual(retries, [2])

    def test_safe_document_reader_rejects_path_escape_and_limits_excerpt(self):
        with self.assertRaises(ValueError):
            _safe_doc_path("..\\..\\outside.txt")
        result = read_document("采购报销流程#0")
        self.assertLessEqual(len(result["excerpt"]), 900)
        full_text = (DOCS_DIR / "采购报销流程.md").read_text(encoding="utf-8")
        self.assertNotEqual(result["excerpt"], full_text)

    def test_tool_results_transfer_and_cache_by_canonical_arguments(self):
        registry = default_registry()
        plan = WorkflowPlan(
            summary="retrieve and read",
            steps=[
                PlannedToolStep(
                    step_id="search",
                    tool_name="search_local_docs",
                    arguments={"query": "报销 发票", "top_k": 2},
                ),
                PlannedToolStep(
                    step_id="read",
                    tool_name="read_local_doc",
                    arguments={"source_id": {"$ref": "search.matches.0.source_id"}},
                    depends_on=["search"],
                ),
            ],
        )
        first, cache, first_events = execute_plan(plan.model_dump(), registry)
        second, cache, second_events = execute_plan(plan.model_dump(), registry, {}, cache)
        self.assertIn("excerpt", second["read"])
        self.assertEqual(len(cache), 2)
        self.assertTrue(any(event["kind"] == "tool_cached" for event in second_events))
        self.assertTrue(all(event.get("event_id") for event in first_events + second_events))

    def test_failed_tool_persists_prior_results_cache_and_events(self):
        registry = ToolRegistry()
        registry.register(ToolSpec(
            "search_local_docs", "search", SearchInput,
            lambda query, top_k=4: {"matches": [{"source_id": "doc#0", "excerpt": "摘要"}]},
            **LOW_POLICY,
        ))
        registry.register(ToolSpec(
            "read_local_doc", "read", ReadInput,
            lambda source_id: (_ for _ in ()).throw(RuntimeError("read failed")),
            max_attempts=1,
            **LOW_POLICY,
        ))
        plan = WorkflowPlan(
            summary="search then read",
            steps=[
                PlannedToolStep(step_id="search", tool_name="search_local_docs", arguments={"query": "x"}),
                PlannedToolStep(
                    step_id="read", tool_name="read_local_doc",
                    arguments={"source_id": {"$ref": "search.matches.0.source_id"}},
                    depends_on=["search"],
                ),
            ],
        )
        with self.assertRaises(ToolExecutionError) as caught:
            execute_plan(plan.model_dump(), registry)
        self.assertIn("search", caught.exception.results)
        self.assertEqual(len(caught.exception.cache), 1)
        self.assertEqual(caught.exception.events[-1]["kind"], "tool_failed")

    def test_agent_rejects_report_source_not_approved(self):
        invalid = agents.ReportNarrative(
            title="报告",
            summary="有依据的摘要",
            summary_source_ids=["missing#0"],
            claims=[],
        )
        with patch("workflow.agents.invoke_structured", return_value=(invalid, {})):
            with self.assertRaises(ValueError):
                agents.write_report(
                    "任务",
                    {"findings": [{"supported": True, "source_ids": ["valid#0"]}]},
                    {"valid#0": "支持摘要的摘录"},
                )

    def test_timeout_is_bounded_for_retrying_read_tool(self):
        registry = ToolRegistry()

        def slow(value: str):
            time.sleep(0.08)
            return {"value": value}

        registry.register(ToolSpec(
            "slow", "slow", EchoInput, slow, timeout_seconds=0.01, max_attempts=1, **LOW_POLICY,
        ))
        started = time.perf_counter()
        with self.assertRaises(TimeoutError):
            registry.invoke("slow", {"value": "x"})
        self.assertLess(time.perf_counter() - started, 0.06)

    def test_supplier_registry_filters_with_stable_source_ids(self):
        by_id = query_supplier_registry(supplier_id="SUP-1002")
        self.assertEqual(by_id["matched"], 1)
        record = by_id["records"][0]
        self.assertEqual(record["source_id"], "supplier:SUP-1002")
        self.assertEqual(record["name"], "华星科技服务有限公司")
        self.assertEqual(record["status"], "pending")

        by_name = query_supplier_registry(name="华星")
        self.assertEqual(by_name, by_id)

        by_status = query_supplier_registry(status="active")
        self.assertTrue(by_status["records"])
        self.assertTrue(all(row["status"] == "active" for row in by_status["records"]))
        self.assertEqual(by_status, query_supplier_registry(status="active"))

    def test_supplier_registry_rejects_invalid_filters_and_limits_summaries(self):
        with self.assertRaises(ValidationError):
            SupplierQueryInput(status="unknown")
        with self.assertRaises(ValidationError):
            SupplierQueryInput(supplier_id="SUP 1002")
        with self.assertRaises(ValidationError):
            SupplierQueryInput(supplier_id="SUP-1002", unexpected=True)

        records = query_supplier_registry()["records"]
        self.assertLessEqual(len(records), MAX_RETURNED_RECORDS)
        self.assertTrue(records)
        for record in records:
            self.assertLessEqual(len(record["excerpt"]), MAX_ROW_EXCERPT_CHARS)
            self.assertTrue(record["excerpt"].startswith(f"供应商 {record['supplier_id']}"))

    def test_supplier_registry_is_readonly_and_feeds_source_excerpts(self):
        spec = default_registry().get("query_supplier_registry")
        self.assertTrue(spec.read_only)
        self.assertTrue(spec.idempotent)

        result = default_registry().invoke(
            "query_supplier_registry", {"name": "华星"}
        )
        excerpts = source_excerpts([result])
        self.assertIn("supplier:SUP-1002", excerpts)
        self.assertLessEqual(len(excerpts["supplier:SUP-1002"]), MAX_ROW_EXCERPT_CHARS)

        with self.assertRaises(ValidationError):
            default_registry().invoke(
                "query_supplier_registry", {"status": "invalid"}
            )

    def test_three_step_supplier_plan_transfers_category_and_reuses_cache(self):
        registry = default_registry()
        plan = WorkflowPlan(
            summary="supplier then policy",
            steps=[
                PlannedToolStep(
                    step_id="suppliers",
                    tool_name="query_supplier_registry",
                    arguments={"supplier_id": "SUP-1002"},
                ),
                PlannedToolStep(
                    step_id="policy_search",
                    tool_name="search_local_docs",
                    arguments={"query": {"$ref": "suppliers.records.0.category"}, "top_k": 2},
                    depends_on=["suppliers"],
                ),
                PlannedToolStep(
                    step_id="policy_read",
                    tool_name="read_local_doc",
                    arguments={"source_id": {"$ref": "policy_search.matches.0.source_id"}},
                    depends_on=["policy_search"],
                ),
            ],
        )
        agents._validate_plan(plan, registry)
        first, cache, _ = execute_plan(plan.model_dump(), registry, plan_approved=True)
        self.assertEqual(first["suppliers"]["records"][0]["category"], "办公设备")
        self.assertTrue(first["policy_search"]["matches"])
        self.assertTrue(first["policy_read"]["source_id"].startswith("供应商准入检查#"))

        second, cache, events = execute_plan(plan.model_dump(), registry, {}, cache, plan_approved=True)
        self.assertEqual(second, first)
        self.assertEqual(len(cache), 3)
        self.assertEqual(sum(event["kind"] == "tool_cached" for event in events), 3)

    def test_empty_supplier_lookup_skips_dependent_steps(self):
        registry = default_registry()
        plan = WorkflowPlan(
            summary="missing supplier",
            steps=[
                PlannedToolStep(
                    step_id="suppliers",
                    tool_name="query_supplier_registry",
                    arguments={"name": "不存在的供应商"},
                ),
                PlannedToolStep(
                    step_id="policy_search",
                    tool_name="search_local_docs",
                    arguments={"query": {"$ref": "suppliers.records.0.category"}},
                    depends_on=["suppliers"],
                ),
                PlannedToolStep(
                    step_id="policy_read",
                    tool_name="read_local_doc",
                    arguments={"source_id": {"$ref": "policy_search.matches.0.source_id"}},
                    depends_on=["policy_search"],
                ),
            ],
        )
        results, _, events = execute_plan(plan.model_dump(), registry, plan_approved=True)
        self.assertEqual(results["suppliers"]["records"], [])
        self.assertTrue(results["policy_search"]["_skipped"])
        self.assertTrue(results["policy_read"]["_skipped"])
        self.assertEqual(sum(event["kind"] == "tool_skipped" for event in events), 2)

    def test_tool_registration_requires_complete_policy(self):
        registry = ToolRegistry()
        echo = lambda value: {"value": value}
        with self.assertRaises(ValueError):
            registry.register(ToolSpec("missing", "missing policy", EchoInput, echo))
        with self.assertRaises(ValueError):
            registry.register(ToolSpec(
                "bad_risk", "bad risk", EchoInput, echo,
                risk_level="critical", requires_approval=True, data_scope="internal",
            ))
        with self.assertRaises(ValueError):
            registry.register(ToolSpec(
                "medium_no_gate", "medium without gate", EchoInput, echo,
                risk_level="medium", requires_approval=False, data_scope="internal",
            ))
        with self.assertRaises(ValueError):
            registry.register(ToolSpec(
                "restricted_no_gate", "restricted without gate", EchoInput, echo,
                risk_level="low", requires_approval=False, data_scope="restricted",
            ))
        registry.register(ToolSpec("ok", "ok", EchoInput, echo, **LOW_POLICY))

    def test_policy_check_allow_deny_needs_approval(self):
        registry = default_registry()
        self.assertEqual(
            policy_check(PlannedToolStep(step_id="s", tool_name="search_local_docs"), registry).action,
            "allow",
        )
        self.assertEqual(
            policy_check(PlannedToolStep(step_id="s", tool_name="query_supplier_registry"), registry).action,
            "needs_approval",
        )
        self.assertEqual(
            policy_check(PlannedToolStep(step_id="s", tool_name="delete_records"), registry).action,
            "deny",
        )
        registry.register(ToolSpec(
            "danger", "danger", EchoInput, lambda value: {"value": value},
            risk_level="high", requires_approval=True, data_scope="internal",
        ))
        self.assertEqual(
            policy_check(PlannedToolStep(step_id="s", tool_name="danger"), registry).action,
            "deny",
        )

    def test_plan_approval_reason_is_deterministic(self):
        registry = default_registry()
        supplier_plan = WorkflowPlan(
            summary="supplier",
            steps=[PlannedToolStep(step_id="s", tool_name="query_supplier_registry")],
        )
        reason = plan_approval_reason(supplier_plan, registry, "整理供应商资料")
        self.assertIn("query_supplier_registry", reason)
        low_plan = WorkflowPlan(
            summary="low",
            steps=[PlannedToolStep(step_id="s", tool_name="search_local_docs")],
        )
        self.assertEqual(plan_approval_reason(low_plan, registry, "整理采购资料"), "")
        self.assertIn("合同签署", plan_approval_reason(low_plan, registry, "核对合同签署材料"))

    def test_execute_plan_denies_unregistered_tool_and_records_event(self):
        plan = WorkflowPlan(
            summary="deny",
            steps=[PlannedToolStep(step_id="delete", tool_name="delete_records")],
        )
        with self.assertRaises(PolicyDeniedError) as caught:
            execute_plan(plan.model_dump(), default_registry(), plan_approved=True)
        self.assertTrue(any(
            event["kind"] == "policy_check" and event["status"] == "deny"
            for event in caught.exception.events
        ))

    def test_execute_plan_requires_approval_for_medium_tool(self):
        plan = WorkflowPlan(
            summary="supplier",
            steps=[PlannedToolStep(
                step_id="suppliers", tool_name="query_supplier_registry",
                arguments={"supplier_id": "SUP-1002"},
            )],
        )
        with self.assertRaises(ApprovalRequiredError):
            execute_plan(plan.model_dump(), default_registry())
        results, _, events = execute_plan(plan.model_dump(), default_registry(), plan_approved=True)
        self.assertEqual(results["suppliers"]["records"][0]["source_id"], "supplier:SUP-1002")
        self.assertTrue(any(event["kind"] == "policy_check" for event in events))

    def test_audit_chain_detects_tampering(self):
        events = chain_events([], [
            {"event_id": "a", "kind": "task_started", "status": "running", "detail": "start", "at": "2026-01-01T00:00:00+00:00"},
            {"event_id": "b", "kind": "tool_started", "status": "running", "detail": "tool", "at": "2026-01-01T00:00:01+00:00"},
            {"event_id": "c", "kind": "tool_completed", "status": "completed", "detail": "done", "at": "2026-01-01T00:00:02+00:00"},
        ])
        self.assertEqual([event["seq"] for event in events], [1, 2, 3])
        self.assertTrue(verify_audit_chain(events)["valid"])
        tampered = [dict(event) for event in events]
        tampered[1]["detail"] = "tampered"
        result = verify_audit_chain(tampered)
        self.assertFalse(result["valid"])
        self.assertEqual(result["broken_at"], 2)

    def test_metrics_from_events_and_llm_calls(self):
        events = chain_events([], [
            {"event_id": "a", "kind": "tool_started", "status": "running", "detail": "", "at": "2026-01-01T00:00:00+00:00"},
            {"event_id": "b", "kind": "tool_cached", "status": "completed", "detail": "", "at": "2026-01-01T00:00:00.100000+00:00"},
            {"event_id": "c", "kind": "tool_completed", "status": "completed", "detail": "", "at": "2026-01-01T00:00:00.200000+00:00"},
            {"event_id": "d", "kind": "tool_retry", "status": "retrying", "detail": "", "at": "2026-01-01T00:00:00.300000+00:00"},
            {"event_id": "e", "kind": "policy_check", "status": "deny", "detail": "", "at": "2026-01-01T00:00:00.400000+00:00"},
        ])
        state = {
            "run_id": "metrics",
            "status": "succeeded",
            "events": events,
            "llm_calls": [
                {"role": "Planner", "elapsed_ms": 10, "input_tokens": 5, "output_tokens": 3, "total_tokens": 8},
                {"role": "Review Agent", "elapsed_ms": 4, "input_tokens": None, "output_tokens": None, "total_tokens": None},
                {"role": "Report Agent", "elapsed_ms": 6, "input_tokens": None, "output_tokens": None, "total_tokens": None},
            ],
            "approval_history": [{"stage": "plan"}, {"stage": "report"}],
        }
        metrics = compute_metrics(state)
        self.assertEqual(metrics["tool_calls"], 1)
        self.assertEqual(metrics["tool_cached"], 1)
        self.assertEqual(metrics["tool_completed"], 1)
        self.assertEqual(metrics["cache_hit_rate"], 0.5)
        self.assertEqual(metrics["retries"], 1)
        self.assertEqual(metrics["policy_denials"], 1)
        self.assertEqual(metrics["plan_gate_visits"], 1)
        self.assertEqual(metrics["report_gate_visits"], 1)
        self.assertEqual(metrics["llm_calls"], 3)
        self.assertEqual(metrics["token_usage"], {"input_tokens": 5, "output_tokens": 3, "total_tokens": 8})
        self.assertEqual(metrics["node_durations_ms"]["planner"], 10.0)
        self.assertEqual(metrics["node_durations_ms"]["review"], 4.0)
        self.assertEqual(metrics["node_durations_ms"]["report"], 6.0)
        self.assertEqual(metrics["node_durations_ms"]["data_agent"], 400.0)
        self.assertTrue(metrics["audit_valid"])

    def test_claim_support_strong_weak_and_no_citation(self):
        sources = {"supplier:SUP-1001": "供应商 SUP-1001 远山办公设备有限公司，状态为合作中。"}
        strong = evaluate_claim("供应商 SUP-1001 远山办公设备有限公司", ["supplier:SUP-1001"], sources)
        self.assertEqual(strong["status"], "strong")
        weak = evaluate_claim("本周天气预报显示上海多云转晴", ["supplier:SUP-1001"], sources)
        self.assertEqual(weak["status"], "weak")
        no_citation = evaluate_claim("未引用来源的声明", [], sources)
        self.assertEqual(no_citation["status"], "no_citation")

    def test_report_support_marks_evidence_gap_not_applicable(self):
        support = evaluate_report_support(
            {"summary": "未找到资料", "summary_source_ids": [], "claims": []},
            {},
        )
        self.assertEqual(support["summary"]["status"], "not_applicable")
        self.assertFalse(support["needs_review"])


class WorkflowIntegrationTests(unittest.TestCase):
    def setUp(self):
        from langgraph.checkpoint.sqlite import SqliteSaver

        self.saver_context = SqliteSaver.from_conn_string(":memory:")
        self.saver = self.saver_context.__enter__()
        self.graph = build_graph(self.saver)
        self.mock_env = patch.dict(os.environ, {"WORKFLOW_MOCK_LLM": "1"})
        self.mock_env.start()

    def tearDown(self):
        self.mock_env.stop()
        self.saver_context.__exit__(None, None, None)

    def test_return_replans_reuses_cached_tools_then_approval_completes(self):
        config = {"configurable": {"thread_id": "full-cycle"}}
        self.graph.invoke({"run_id": "full-cycle", "task": "整理采购报销资料"}, config=config)
        state = self.graph.get_state(config).values
        self.assertEqual(state["status"], "awaiting_human")
        self.assertFalse(any(
            entry.get("stage") == "plan" for entry in state["approval_history"]
        ))
        self.assertTrue(state["source_excerpts"])
        self.assertNotIn("document_text", state)
        self.assertNotIn("csv_text", state)
        self.assertLessEqual(max(map(len, state["source_excerpts"].values())), 900)

        self.graph.invoke(Command(resume={"decision": "return", "comment": "补充依据"}), config=config)
        state = self.graph.get_state(config).values
        self.assertEqual(state["status"], "awaiting_human")
        self.assertEqual(state["return_count"], 1)
        self.assertTrue(any(event["kind"] == "tool_cached" for event in state["events"]))

        self.graph.invoke(Command(resume={"decision": "approve", "comment": "确认"}), config=config)
        state = self.graph.get_state(config).values
        self.assertEqual(state["status"], "succeeded")
        self.assertTrue(state["report"]["summary_source_ids"])
        event_ids = [event["event_id"] for event in state["events"]]
        self.assertEqual(len(event_ids), len(set(event_ids)))

    def test_return_limit_terminates_and_does_not_create_report(self):
        config = {"configurable": {"thread_id": "return-limit"}}
        self.graph.invoke({"run_id": "return-limit", "task": "整理安全事件资料"}, config=config)
        self.graph.invoke(Command(resume={"decision": "return", "comment": "再查"}), config=config)
        self.graph.invoke(Command(resume={"decision": "return", "comment": "仍需补充"}), config=config)
        state = self.graph.get_state(config).values
        self.assertEqual(state["status"], "terminated")
        self.assertNotIn("report", state)

    def test_supplier_three_step_flow_returns_replans_and_completes(self):
        config = {"configurable": {"thread_id": "supplier-flow"}}
        self.graph.invoke(
            {"run_id": "supplier-flow", "task": "整理供应商华星的准入资料"},
            config=config,
        )
        state = self.graph.get_state(config).values
        self.assertEqual(state["status"], "awaiting_plan_approval")
        self.assertIn("query_supplier_registry", state["plan_gate_reason"])
        self.assertFalse(state["plan_approved"])
        self.assertEqual(
            [step["tool_name"] for step in state["plan"]["steps"]],
            ["query_supplier_registry", "search_local_docs", "read_local_doc"],
        )

        self.graph.invoke(
            Command(resume={"decision": "approve", "comment": "批准计划"}),
            config=config,
        )
        state = self.graph.get_state(config).values
        self.assertEqual(state["status"], "awaiting_human")
        self.assertTrue(any(
            entry.get("stage") == "plan" for entry in state["approval_history"]
        ))
        self.assertIn("supplier:SUP-1002", state["source_excerpts"])
        self.assertTrue(
            any(source_id.startswith("供应商准入检查#") for source_id in state["source_excerpts"])
        )
        self.assertTrue(state["questions"])

        self.graph.invoke(
            Command(resume={"decision": "return", "comment": "请补充依据"}),
            config=config,
        )
        state = self.graph.get_state(config).values
        self.assertEqual(state["status"], "awaiting_plan_approval")
        self.assertEqual(state["return_count"], 1)

        self.graph.invoke(
            Command(resume={"decision": "approve", "comment": "批准新计划"}),
            config=config,
        )
        state = self.graph.get_state(config).values
        self.assertEqual(state["status"], "awaiting_human")
        self.assertTrue(any(event["kind"] == "tool_cached" for event in state["events"]))

        self.graph.invoke(
            Command(resume={"decision": "approve", "comment": "确认"}),
            config=config,
        )
        state = self.graph.get_state(config).values
        self.assertEqual(state["status"], "succeeded")
        self.assertTrue(state["report"]["summary_source_ids"])
        self.assertTrue(
            set(state["report"]["summary_source_ids"]).issubset(state["source_excerpts"])
        )
        self.assertIn(state["support_review"]["summary"]["status"], {"strong", "weak", "no_citation"})
        self.assertTrue(state["support_review"]["claims"])
        audit = verify_audit_chain(state["events"])
        self.assertTrue(audit["valid"])
        self.assertEqual(audit["events_checked"], len(state["events"]))
        trace = build_export(state)
        self.assertTrue(trace["audit"]["valid"])
        self.assertGreaterEqual(trace["metrics"]["tool_calls"], 3)
        markdown = render_markdown(trace)
        self.assertIn("最终报告", markdown)
        self.assertIn("审批历史", markdown)
        self.assertIn("审计链", markdown)
        self.assertIn("引用支持度", markdown)

    def test_plan_gate_return_replans_before_execution(self):
        config = {"configurable": {"thread_id": "plan-return"}}
        self.graph.invoke(
            {"run_id": "plan-return", "task": "整理供应商华星的准入资料"},
            config=config,
        )
        self.assertEqual(self.graph.get_state(config).values["status"], "awaiting_plan_approval")
        self.graph.invoke(
            Command(resume={"decision": "return", "comment": "请先核对供应商范围"}),
            config=config,
        )
        state = self.graph.get_state(config).values
        self.assertEqual(state["status"], "awaiting_plan_approval")
        self.assertEqual(state["plan_return_count"], 1)
        self.assertEqual(state["plan_approved"], False)
        self.assertEqual(state["tool_results"], {})
        self.assertTrue(any(
            entry.get("stage") == "plan" and entry.get("decision") == "return"
            for entry in state["approval_history"]
        ))

        self.graph.invoke(
            Command(resume={"decision": "approve", "comment": "批准"}),
            config=config,
        )
        self.assertEqual(self.graph.get_state(config).values["status"], "awaiting_human")

    def test_empty_search_skips_dependent_read_and_reports_evidence_gap(self):
        config = {"configurable": {"thread_id": "empty-search"}}
        self.graph.invoke({"run_id": "empty-search", "task": "㐀㐁㐂㐃"}, config=config)
        state = self.graph.get_state(config).values
        self.assertEqual(state["status"], "awaiting_human")
        self.assertTrue(any(event["kind"] == "tool_skipped" for event in state["events"]))
        self.graph.invoke(Command(resume={"decision": "approve", "comment": "确认缺少资料"}), config=config)
        state = self.graph.get_state(config).values
        self.assertEqual(state["status"], "succeeded")
        self.assertEqual(state["report"]["summary_source_ids"], [])
        self.assertEqual(state["report"]["claims"], [])

    def test_api_submit_status_tools_and_approval(self):
        from fastapi.testclient import TestClient
        from workflow.api import app

        with patch.dict(os.environ, {
            "WORKFLOW_MOCK_LLM": "1",
            "WORKFLOW_CHECKPOINT_PATH": ":memory:",
        }):
            with TestClient(app) as client:
                self.assertEqual(client.get("/health").json()["service"], "enterprise-workflow-platform")
                tools = client.get("/tools").json()["tools"]
                self.assertEqual(
                    {item["name"] for item in tools},
                    {"search_local_docs", "read_local_doc", "query_supplier_registry"},
                )
                self.assertTrue(all(item["risk_level"] for item in tools))
                self.assertTrue(all(item["data_scope"] == "internal" for item in tools))

                run_id = client.post("/runs", json={"task": "整理采购报销材料"}).json()["run_id"]
                state = client.get(f"/runs/{run_id}").json()
                self.assertEqual(state["status"], "awaiting_human")
                self.assertEqual(state["approval_stage"], "report")
                self.assertIn("questions", state)
                self.assertTrue(state["questions"])

                supplier_run_id = client.post(
                    "/runs", json={"task": "整理供应商华星的准入资料"}
                ).json()["run_id"]
                supplier_state = client.get(f"/runs/{supplier_run_id}").json()
                self.assertEqual(supplier_state["status"], "awaiting_plan_approval")
                self.assertEqual(supplier_state["approval_stage"], "plan")
                self.assertEqual(len(supplier_state["plan"]["steps"]), 3)
                self.assertTrue(supplier_state["plan_gate_reason"])

                plan_approved = client.post(
                    f"/runs/{supplier_run_id}/approval",
                    json={"decision": "approve", "comment": "批准计划"},
                )
                self.assertEqual(plan_approved.status_code, 202)
                self.assertEqual(plan_approved.json()["approval_stage"], "plan")
                supplier_state = client.get(f"/runs/{supplier_run_id}").json()
                self.assertEqual(supplier_state["status"], "awaiting_human")
                self.assertEqual(supplier_state["approval_stage"], "report")
                self.assertTrue(supplier_state["questions"])

                report_approved = client.post(
                    f"/runs/{supplier_run_id}/approval",
                    json={"decision": "approve", "comment": "确认"},
                )
                self.assertEqual(report_approved.status_code, 202)
                supplier_state = client.get(f"/runs/{supplier_run_id}").json()
                self.assertEqual(supplier_state["status"], "succeeded")

                approved = client.post(
                    f"/runs/{run_id}/approval",
                    json={"decision": "approve", "comment": "确认"},
                )
                self.assertEqual(approved.status_code, 202)
                state = client.get(f"/runs/{run_id}").json()
                self.assertEqual(state["status"], "succeeded")

                metrics = client.get(f"/runs/{run_id}/metrics").json()
                self.assertGreaterEqual(metrics["tool_calls"], 2)
                self.assertTrue(metrics["audit_valid"])
                audit = client.get(f"/runs/{run_id}/audit/verify").json()
                self.assertTrue(audit["valid"])
                self.assertEqual(audit["events_checked"], metrics["event_count"])

                markdown = client.get(f"/runs/{run_id}/export", params={"format": "md"})
                self.assertEqual(markdown.status_code, 200)
                self.assertIn("最终报告", markdown.text)
                payload = client.get(f"/runs/{run_id}/export", params={"format": "json"})
                self.assertEqual(payload.status_code, 200)
                exported = payload.json()
                for key in ("plan", "events", "metrics", "audit"):
                    self.assertIn(key, exported)
                self.assertTrue(exported["audit"]["valid"])
                self.assertEqual(
                    client.get("/runs/not-found/metrics").status_code,
                    404,
                )
                self.assertEqual(
                    client.get(f"/runs/{run_id}/export", params={"format": "xml"}).status_code,
                    422,
                )


if __name__ == "__main__":
    unittest.main()
