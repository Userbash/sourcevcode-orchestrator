from __future__ import annotations
from core.agents.external_worker_agent import ExternalWorkerAgent
from core.core.models import AgentResult, Task, TaskStatus

class DesignAgent(ExternalWorkerAgent):
    def __init__(self, agent_id: str = "design_agent") -> None:
        super().__init__(agent_id, ["design_conceptualization", "style_guide_generation", "ux_strategy"], "design")

class FrontendComponentAgent(ExternalWorkerAgent):
    def __init__(self, agent_id: str = "frontend_component_agent") -> None:
        super().__init__(agent_id, ["react_component_development", "tailwind_styling", "semantic_html"], "frontend")

class UXValidatorAgent(ExternalWorkerAgent):
    def __init__(self, agent_id: str = "ux_validator_agent") -> None:
        super().__init__(agent_id, ["ux_heuristics_audit", "accessibility_audit", "usability_testing"], "ux")

