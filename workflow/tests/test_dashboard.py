from __future__ import annotations

import os
import unittest
from pathlib import Path
from unittest.mock import patch

from streamlit.testing.v1 import AppTest

from workflow.dashboard import SCENARIOS, _phase_states


DASHBOARD_PATH = Path(__file__).resolve().parents[1] / "dashboard.py"


class FakeResponse:
    def __init__(self, payload=None, content=b"demo"):
        self._payload = payload or {}
        self.content = content
        self.status_code = 200

    def json(self):
        return self._payload

    def raise_for_status(self):
        return None


def _completed_state():
    source_id = "supplier:SUP-1002"
    return {
        "run_id": "demo-run",
        "task": SCENARIOS["供应商准入核验"]["task"],
        "status": "succeeded",
        "plan": {
            "summary": "查询登记记录并依据准入制度核验。",
            "steps": [{"step_id": "lookup", "tool_name": "query_supplier_registry", "arguments": {}, "depends_on": []}],
        },
        "step_policies": {"lookup": {"risk_level": "medium", "data_scope": "internal", "action": "needs_approval"}},
        "tool_results": {"lookup": {"records": [{"source_id": source_id, "supplier_id": "SUP-1002", "name": "华星科技服务有限公司", "category": "办公设备", "status": "pending"}]}},
        "source_excerpts": {source_id: "华星科技服务有限公司；类别：办公设备；状态：待审核。"},
        "review": {"summary": "已对照登记信息与制度资料。", "findings": [{"statement": "供应商当前状态为待审核。", "source_ids": [source_id], "supported": True}], "questions": ["请人工确认资质材料是否齐全。"], "limitations": []},
        "questions": ["请人工确认资质材料是否齐全。"],
        "report": {"title": "供应商准入核验报告", "summary": "华星科技服务有限公司当前登记状态为待审核。", "summary_source_ids": [source_id], "claims": [{"text": "供应商登记类别为办公设备。", "source_ids": [source_id]}], "limitations": ["是否满足准入要求仍需人工核实。"]},
        "support_review": {},
        "approval_history": [],
        "events": [{"event_id": "evt-1", "kind": "report_completed", "status": "succeeded", "detail": "报告已生成", "at": "2026-10-23T10:00:00+00:00"}],
        "llm_calls": [],
    }


def _fake_request(method, url, **kwargs):
    if url.endswith("/runs/demo-run"):
        return FakeResponse(_completed_state())
    if url.endswith("/metrics"):
        return FakeResponse({"tool_calls": 3, "tool_cached": 1, "retries": 0, "token_usage": {"total_tokens": 1200}, "event_span_ms": 4000})
    if url.endswith("/audit/verify"):
        return FakeResponse({"valid": True})
    if url.endswith("/export"):
        return FakeResponse(content=b"demo export")
    if url.endswith("/tools"):
        return FakeResponse({"tools": []})
    raise AssertionError(f"unexpected request: {method} {url}")


class DashboardTests(unittest.TestCase):
    def test_phase_indicators_reflect_plan_and_human_approval_gates(self):
        waiting_for_plan = {
            "status": "awaiting_plan_approval",
            "approval_stage": "plan",
            "plan": {"steps": [{"step_id": "s1"}]},
        }
        phases = {name: status for name, status, _ in _phase_states(waiting_for_plan)}
        self.assertEqual(phases["制定计划"], "done")
        self.assertEqual(phases["查询资料"], "waiting")
        self.assertNotEqual(phases["生成报告"], "done")

        waiting_for_report = {
            "status": "awaiting_human",
            "approval_stage": "report",
            "plan": {"steps": [{"step_id": "s1"}]},
            "tool_results": {"s1": {"matches": []}},
            "review": {"findings": []},
        }
        phases = {name: status for name, status, _ in _phase_states(waiting_for_report)}
        self.assertEqual(phases["人工确认"], "current")
        self.assertEqual(phases["审核依据"], "done")

    def test_landing_explains_business_value_and_scenario_switch_updates_task(self):
        with patch.dict(os.environ, {"WORKFLOW_API_URL": "http://127.0.0.1:65503"}):
            with patch("httpx.request", side_effect=_fake_request):
                app = AppTest.from_file(str(DASHBOARD_PATH), default_timeout=20).run()
                self.assertFalse(app.exception)
                self.assertIn("供应商准入核验", app.title[0].value)
                self.assertEqual(len(app.radio), 1)
                app.radio[0].set_value("报销资料整理").run()
                self.assertFalse(app.exception)
                self.assertIn("报销申请", app.text_area[0].value)

    def test_completed_run_shows_human_readable_report_sources_and_metrics(self):
        with patch.dict(os.environ, {"WORKFLOW_API_URL": "http://127.0.0.1:65504"}):
            with patch("httpx.request", side_effect=_fake_request):
                app = AppTest.from_file(str(DASHBOARD_PATH), default_timeout=20)
                app.session_state["active_run_id"] = "demo-run"
                app.session_state["active_scenario"] = "供应商准入核验"
                app.run()
                self.assertFalse(app.exception)
                rendered = "\n".join(node.value for node in app.markdown)
                self.assertIn("供应商准入核验报告", rendered)
                self.assertIn("华星科技服务有限公司", rendered)
                self.assertIn("本次运行数据", rendered)
                self.assertTrue(any("导出本次运行审计包" in item.label for item in app.expander))


if __name__ == "__main__":
    unittest.main()
