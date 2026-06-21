from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, List, Optional

from pydantic import BaseModel, Field, model_validator

from .kernel_protocol import KernelAPI, KernelModule
from .models import Task, Complexity, Priority

logger = logging.getLogger("risk_advisor")

_RISK_SCORE_BY_LEVEL = {
    "low": 0.2,
    "medium": 0.5,
    "high": 0.8,
    "critical": 0.95,
}
_REVIEW_LEVEL_BY_RISK = {
    "low": "peer",
    "medium": "senior",
    "high": "architect",
    "critical": "architect",
}

class RiskAssessment(BaseModel):
    risk_score: float = Field(description="Risk score from 0.0 to 1.0")
    complexity_level: str = Field(description="low, medium, high, critical")
    justification: str
    impact_areas: List[str]
    suggested_review_level: str = Field(description="none, peer, senior, architect")

    @staticmethod
    def _first_text(*values: Any) -> str:
        for value in values:
            if isinstance(value, str) and value.strip():
                return value.strip()
        return ""

    @staticmethod
    def _string_list(value: Any) -> List[str]:
        if isinstance(value, list):
            return [str(item).strip() for item in value if str(item).strip()]
        if isinstance(value, str) and value.strip():
            return [value.strip()]
        return []

    @classmethod
    def _normalize_level(cls, value: Any) -> str:
        text = str(value or "").strip().lower()
        if text in _RISK_SCORE_BY_LEVEL:
            return text
        if "critical" in text:
            return "critical"
        if "high" in text:
            return "high"
        if "medium" in text:
            return "medium"
        if "low" in text:
            return "low"
        return "medium"

    @classmethod
    def _normalize_score(cls, value: Any, level: str) -> float:
        try:
            score = float(value)
            return max(0.0, min(1.0, score))
        except (TypeError, ValueError):
            return _RISK_SCORE_BY_LEVEL.get(level, 0.5)

    @model_validator(mode="before")
    @classmethod
    def _coerce_fallback_payload(cls, value: Any) -> Any:
        if not isinstance(value, dict):
            return value
        if all(key in value for key in ("risk_score", "complexity_level", "justification", "impact_areas", "suggested_review_level")):
            return value

        risk_payload = value.get("risk") if isinstance(value.get("risk"), dict) else {}
        level = cls._normalize_level(
            value.get("complexity_level")
            or value.get("risk_level")
            or value.get("riskLevel")
            or risk_payload.get("level")
            or risk_payload.get("type")
            or value.get("severity")
        )
        justification = cls._first_text(
            value.get("justification"),
            value.get("reasoning"),
            value.get("explanation"),
            value.get("summary"),
            value.get("potential_impact"),
            value.get("potentialImpact"),
            risk_payload.get("justification"),
            risk_payload.get("reason"),
        ) or f"Fallback risk assessment normalized from AI response with level={level}."
        impact_areas = (
            cls._string_list(value.get("impact_areas"))
            or cls._string_list(value.get("impactAreas"))
            or cls._string_list(value.get("areas"))
            or cls._string_list(value.get("recommendations"))
            or cls._string_list(risk_payload.get("impact_areas"))
            or cls._string_list(risk_payload.get("areas"))
        )
        review_level = cls._normalize_level(
            value.get("suggested_review_level")
            or value.get("suggestedReviewLevel")
            or value.get("review_level")
            or value.get("reviewLevel")
        )
        normalized_review = review_level if review_level in {"none", "peer", "senior", "architect"} else _REVIEW_LEVEL_BY_RISK.get(level, "senior")
        return {
            "risk_score": cls._normalize_score(value.get("risk_score") or risk_payload.get("score"), level),
            "complexity_level": level,
            "justification": justification,
            "impact_areas": impact_areas or ["general_architecture"],
            "suggested_review_level": normalized_review,
        }

@dataclass
class RiskAdvisorModule:
    name: str = "risk_advisor"
    _api: KernelAPI | None = None

    def on_load(self, api: KernelAPI) -> None:
        self._api = api
        self._api.log("info", f"[RISK] {self.name} module loaded.")

    def on_unload(self) -> None:
        pass

    def evaluate_task(self, task: Task) -> Optional[RiskAssessment]:
        reasoning = self._api.get_module("reasoning") if self._api else None
        if not reasoning:
            return None

        prompt = f"""Evaluate the risk of the following task:
Task Type: {task.type.value}
Description: {task.input.description}
Files: {', '.join(task.input.files)}
Context: {task.context.repo_path}
"""
        
        system_prompt = "You are a senior security and architecture auditor. Provide a structured risk assessment."
        
        # Use a "Thinking" model if possible, otherwise default.
        model = "gpt-5.5" # default reasoning model
        
        return reasoning.structured_call(prompt, RiskAssessment, system_prompt=system_prompt, model=model)

    def finalize(self) -> dict[str, Any]:
        return {"status": "ready"}
