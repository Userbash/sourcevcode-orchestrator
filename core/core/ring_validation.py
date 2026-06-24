from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class ValidationCheck(BaseModel):
    status: str
    meta: dict[str, Any] = Field(default_factory=dict)
    coverage_pct: int = 0
    comments: list[str] = Field(default_factory=list)


class ValidationRing(BaseModel):
    security_gate: ValidationCheck
    tester: ValidationCheck
    reviewer: ValidationCheck


class OrchestrationReport(BaseModel):
    task_id: str
    status: str
    execution_dag: dict[str, Any]
    validation_ring: ValidationRing
    quorum_verified: bool
    fix_attempts_spent: int
    final_merged_result: dict[str, Any] = Field(default_factory=dict)
