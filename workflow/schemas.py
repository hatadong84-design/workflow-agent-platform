from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


WorkflowStatus = Literal[
    "running",
    "awaiting_plan_approval",
    "awaiting_human",
    "succeeded",
    "failed",
    "terminated",
]


class PlannedToolStep(BaseModel):
    model_config = ConfigDict(extra="forbid")

    step_id: str = Field(pattern=r"^[a-zA-Z][a-zA-Z0-9_-]{0,63}$")
    tool_name: str = Field(min_length=1, max_length=100)
    arguments: dict[str, Any] = Field(default_factory=dict)
    depends_on: list[str] = Field(default_factory=list)


class WorkflowPlan(BaseModel):
    model_config = ConfigDict(extra="forbid")

    summary: str = Field(min_length=1, max_length=1000)
    steps: list[PlannedToolStep] = Field(min_length=1, max_length=6)

    @model_validator(mode="after")
    def validate_dependency_graph(self) -> "WorkflowPlan":
        ids = [step.step_id for step in self.steps]
        if len(ids) != len(set(ids)):
            raise ValueError("计划步骤 ID 不能重复")
        known = set(ids)
        dependencies = {step.step_id: step.depends_on for step in self.steps}
        for step in self.steps:
            if step.step_id in step.depends_on:
                raise ValueError(f"步骤 {step.step_id} 不能依赖自身")
            unknown = set(step.depends_on) - known
            if unknown:
                raise ValueError(f"步骤 {step.step_id} 依赖了不存在的步骤：{sorted(unknown)}")
            if len(step.depends_on) != len(set(step.depends_on)):
                raise ValueError(f"步骤 {step.step_id} 的依赖不能重复")

        active: set[str] = set()
        visited: set[str] = set()

        def visit(step_id: str) -> None:
            if step_id in active:
                raise ValueError("计划步骤存在循环依赖")
            if step_id in visited:
                return
            active.add(step_id)
            for parent in dependencies[step_id]:
                visit(parent)
            active.remove(step_id)
            visited.add(step_id)

        for step_id in ids:
            visit(step_id)
        return self


class ReviewFinding(BaseModel):
    model_config = ConfigDict(extra="forbid")

    statement: str = Field(min_length=1, max_length=2000)
    source_ids: list[str] = Field(default_factory=list)
    supported: bool = True


class ReviewResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    summary: str = Field(min_length=1, max_length=3000)
    findings: list[ReviewFinding] = Field(default_factory=list, max_length=30)
    questions: list[str] = Field(default_factory=list, max_length=20)
    limitations: list[str] = Field(default_factory=list, max_length=20)


class ReportClaim(BaseModel):
    model_config = ConfigDict(extra="forbid")

    text: str = Field(min_length=1, max_length=2000)
    source_ids: list[str] = Field(min_length=1)


class ReportNarrative(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str = Field(min_length=1, max_length=200)
    summary: str = Field(min_length=1, max_length=3000)
    summary_source_ids: list[str] = Field(default_factory=list)
    claims: list[ReportClaim] = Field(default_factory=list, max_length=30)
    limitations: list[str] = Field(default_factory=list, max_length=20)


class CreateRunRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    task: str = Field(min_length=1, max_length=4000)


class ApprovalRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    decision: Literal["approve", "return"]
    comment: str = Field(default="", max_length=2000)
