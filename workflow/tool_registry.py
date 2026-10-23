from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout
from dataclasses import dataclass
from typing import Any, Callable, Literal

from pydantic import BaseModel, ConfigDict, Field

from .retrieval import read_document, search_documents
from .schemas import PlannedToolStep, WorkflowPlan
from .suppliers import SupplierQueryInput, query_supplier_registry


RiskLevel = Literal["low", "medium", "high"]
DataScope = Literal["public", "internal", "restricted"]
PolicyAction = Literal["allow", "deny", "needs_approval"]
SENSITIVE_TASK_KEYWORDS = ("付款", "转账", "合同签署", "个人信息", "权限变更", "删除")


class SearchInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    query: str = Field(min_length=1, max_length=1000)
    top_k: int = Field(default=4, ge=1, le=8)


class ReadInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source_id: str = Field(min_length=1, max_length=200)


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    input_model: type[BaseModel]
    function: Callable[..., dict[str, Any]]
    risk_level: RiskLevel | None = None
    requires_approval: bool | None = None
    data_scope: DataScope | None = None
    timeout_seconds: float = 8.0
    max_attempts: int = 2
    read_only: bool = True
    idempotent: bool = True

    def public_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "input_schema": self.input_model.model_json_schema(),
            "risk_level": self.risk_level,
            "requires_approval": self.requires_approval,
            "data_scope": self.data_scope,
            "timeout_seconds": self.timeout_seconds,
            "max_attempts": self.max_attempts,
            "read_only": self.read_only,
            "idempotent": self.idempotent,
        }


@dataclass(frozen=True)
class PolicyDecision:
    action: PolicyAction
    reason: str
    step_id: str
    tool_name: str
    risk_level: str | None
    data_scope: str | None


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, ToolSpec] = {}

    def register(self, spec: ToolSpec) -> None:
        if not spec.read_only or not spec.idempotent:
            raise ValueError("只允许注册只读且幂等的工具")
        if spec.risk_level is None or spec.requires_approval is None or spec.data_scope is None:
            raise ValueError("工具必须声明 risk_level、requires_approval 和 data_scope")
        if spec.risk_level not in {"low", "medium", "high"}:
            raise ValueError(f"非法风险等级：{spec.risk_level}")
        if spec.data_scope not in {"public", "internal", "restricted"}:
            raise ValueError(f"非法数据范围：{spec.data_scope}")
        if spec.risk_level in {"medium", "high"} and not spec.requires_approval:
            raise ValueError("medium/high 风险工具必须要求计划审批")
        if spec.data_scope == "restricted" and not spec.requires_approval:
            raise ValueError("restricted 数据范围必须要求计划审批")
        if spec.name in self._tools:
            raise ValueError(f"工具已注册：{spec.name}")
        self._tools[spec.name] = spec

    def get(self, name: str) -> ToolSpec:
        try:
            return self._tools[name]
        except KeyError as exc:
            raise ValueError(f"计划调用了未注册工具：{name}") from exc

    def list_public(self) -> list[dict[str, Any]]:
        return [spec.public_dict() for spec in self._tools.values()]

    def validate_call(self, name: str, arguments: dict[str, Any]) -> BaseModel:
        spec = self.get(name)
        return spec.input_model.model_validate(arguments)

    def invoke(
        self,
        name: str,
        arguments: dict[str, Any],
        on_retry: Callable[[int, str], None] | None = None,
    ) -> dict[str, Any]:
        spec = self.get(name)
        parsed = spec.input_model.model_validate(arguments)
        last_error: Exception | None = None
        for attempt in range(1, spec.max_attempts + 1):
            executor = ThreadPoolExecutor(max_workers=1)
            future = executor.submit(spec.function, **parsed.model_dump())
            try:
                result = future.result(timeout=spec.timeout_seconds)
                if not isinstance(result, dict):
                    raise TypeError(f"工具 {name} 必须返回对象")
                executor.shutdown(wait=False, cancel_futures=True)
                return result
            except FutureTimeout as exc:
                last_error = TimeoutError(f"工具 {name} 超过 {spec.timeout_seconds:g} 秒")
                future.cancel()
            except Exception as exc:
                last_error = exc
            finally:
                executor.shutdown(wait=False, cancel_futures=True)
            if attempt < spec.max_attempts:
                if on_retry:
                    on_retry(attempt + 1, str(last_error))
                continue
        assert last_error is not None
        raise last_error


def default_registry() -> ToolRegistry:
    registry = ToolRegistry()
    registry.register(
        ToolSpec(
            name="search_local_docs",
            description="在工作流样例资料目录内以 BM25 搜索相关资料，返回来源 ID 和短摘录。",
            input_model=SearchInput,
            function=search_documents,
            risk_level="low",
            requires_approval=False,
            data_scope="internal",
        )
    )
    registry.register(
        ToolSpec(
            name="read_local_doc",
            description="按搜索返回的 source_id 读取一段样例资料；只允许样例目录内 Markdown/TXT。",
            input_model=ReadInput,
            function=read_document,
            risk_level="low",
            requires_approval=False,
            data_scope="internal",
        )
    )
    registry.register(
        ToolSpec(
            name="query_supplier_registry",
            description=(
                "在本地样例供应商清单中按供应商 ID、名称或状态查询记录，"
                "返回记录字段、稳定 source_id 和长度受限的行摘要。"
            ),
            input_model=SupplierQueryInput,
            function=query_supplier_registry,
            risk_level="medium",
            requires_approval=True,
            data_scope="internal",
        )
    )
    return registry


def policy_check(step: PlannedToolStep, registry: ToolRegistry) -> PolicyDecision:
    """Deterministic default-deny policy for one planned step."""
    try:
        spec = registry.get(step.tool_name)
    except ValueError:
        return PolicyDecision(
            action="deny",
            reason=f"工具 {step.tool_name} 未注册，拒绝执行",
            step_id=step.step_id,
            tool_name=step.tool_name,
            risk_level=None,
            data_scope=None,
        )
    if not spec.read_only or not spec.idempotent:
        return PolicyDecision(
            action="deny",
            reason=f"工具 {spec.name} 不是只读且幂等",
            step_id=step.step_id,
            tool_name=spec.name,
            risk_level=spec.risk_level,
            data_scope=spec.data_scope,
        )
    if spec.risk_level is None or spec.requires_approval is None or spec.data_scope is None:
        return PolicyDecision(
            action="deny",
            reason=f"工具 {spec.name} 未声明完整策略",
            step_id=step.step_id,
            tool_name=spec.name,
            risk_level=spec.risk_level,
            data_scope=spec.data_scope,
        )
    if spec.risk_level == "high":
        return PolicyDecision(
            action="deny",
            reason=f"工具 {spec.name} 为高风险，本期平台禁止执行",
            step_id=step.step_id,
            tool_name=spec.name,
            risk_level=spec.risk_level,
            data_scope=spec.data_scope,
        )
    if spec.requires_approval or spec.risk_level == "medium" or spec.data_scope == "restricted":
        return PolicyDecision(
            action="needs_approval",
            reason=f"工具 {spec.name} 为 {spec.risk_level} 风险，需要计划审批",
            step_id=step.step_id,
            tool_name=spec.name,
            risk_level=spec.risk_level,
            data_scope=spec.data_scope,
        )
    return PolicyDecision(
        action="allow",
        reason=f"工具 {spec.name} 为低风险只读工具",
        step_id=step.step_id,
        tool_name=spec.name,
        risk_level=spec.risk_level,
        data_scope=spec.data_scope,
    )


def plan_approval_reason(plan: WorkflowPlan, registry: ToolRegistry, task: str) -> str:
    """Return a deterministic reason when the plan must wait for human approval."""
    reasons: list[str] = []
    for step in plan.steps:
        decision = policy_check(step, registry)
        if decision.action == "needs_approval":
            reasons.append(f"步骤 {step.step_id}：{decision.reason}")
    hits = [keyword for keyword in SENSITIVE_TASK_KEYWORDS if keyword in (task or "")]
    if hits:
        reasons.append("任务命中敏感关键词：" + "、".join(hits))
    return "；".join(reasons)


def canonical_cache_key(tool_name: str, arguments: dict[str, Any]) -> str:
    return json.dumps(
        {"tool": tool_name, "arguments": arguments},
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
    )


def _lookup_path(value: Any, path: str) -> Any:
    current = value
    for part in path.split(".") if path else []:
        if isinstance(current, list):
            current = current[int(part)]
        elif isinstance(current, dict):
            current = current[part]
        else:
            raise ValueError(f"无法解析依赖结果路径：{path}")
    return current


def resolve_references(value: Any, dependency_results: dict[str, dict]) -> Any:
    if isinstance(value, dict) and set(value) == {"$ref"}:
        reference = value["$ref"]
        if not isinstance(reference, str) or "." not in reference:
            raise ValueError("结果引用格式应为 step_id.output_path")
        step_id, path = reference.split(".", 1)
        if step_id not in dependency_results:
            raise ValueError(f"引用了未完成的依赖步骤：{step_id}")
        return _lookup_path(dependency_results[step_id], path)
    if isinstance(value, dict):
        return {key: resolve_references(item, dependency_results) for key, item in value.items()}
    if isinstance(value, list):
        return [resolve_references(item, dependency_results) for item in value]
    return value
