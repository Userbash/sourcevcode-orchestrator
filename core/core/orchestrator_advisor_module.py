from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, List, Optional

from pydantic import BaseModel

from .kernel_protocol import KernelAPI
from .models import AgentResult, QualityReport, Task

logger = logging.getLogger("orchestrator_advisor")


class SystemOptimization(BaseModel):
    bottlenecks: List[str]
    suggested_routing_tweaks: List[str]
    agent_performance_insights: dict[str, str]
    overall_health_rating: float


class QualityAudit(BaseModel):
    is_sufficient: bool
    missing_elements: List[str]
    architectural_concerns: List[str]
    remediation_steps: Optional[str]


@dataclass
class OrchestratorAdvisorModule:
    name: str = "orchestrator_advisor"
    _api: KernelAPI | None = None

    def on_load(self, api: KernelAPI) -> None:
        self._api = api
        self._api.log("info", f"[ADVISOR] {self.name} module loaded.")

    def on_unload(self) -> None:
        pass

    def audit_quality(self, task: Task, result: AgentResult, report: QualityReport) -> Optional[QualityAudit]:
        reasoning = self._api.get_module("reasoning") if self._api else None
        if not reasoning:
            return None
        summary = ""
        if getattr(result, "output", None) is not None:
            summary = str(result.output.get("summary", "") or "")

        prompt = f"""Audit the quality of the following result against the task requirements:
Task: {task.input.description}
Acceptance Criteria: {', '.join(task.input.acceptance_criteria)}
Result Summary: {summary}
Current Report Issues: {', '.join(report.issues)}
"""

        system_prompt = "You are a lead system architect. Audit the work of an AI agent and ensure it meets production standards."
        return reasoning.structured_call(prompt, QualityAudit, system_prompt=system_prompt, model="gpt-5.5")

    def suggest_optimizations(self, metrics: dict[str, Any]) -> Optional[SystemOptimization]:
        reasoning = self._api.get_module("reasoning") if self._api else None
        if not reasoning:
            return None

        prompt = f"Analyze the current system metrics and suggest optimizations: {metrics}"
        system_prompt = "You are a high-level AI operations manager. Analyze metrics and provide strategic routing and agent management advice."
        return reasoning.structured_call(prompt, SystemOptimization, system_prompt=system_prompt, model="gpt-5.5")

    def finalize(self) -> dict[str, Any]:
        return {"status": "ready"}
