from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, List, Optional

try:
    import core.core.fix_imports  # noqa: F401
except ImportError:
    pass

from pydantic import BaseModel, Field

from .kernel_protocol import KernelAPI, KernelModule
from .models import Task, TaskType, Priority, ExecutionPlan, TaskInput, TaskContext

logger = logging.getLogger("smart_decomposer")

class SubTask(BaseModel):
    title: str
    description: str
    task_type: str = Field(description="One of: plan, code, review, test, docs, fix, research")
    priority: str = Field(default="normal")
    dependencies: List[str] = Field(default_factory=list, description="IDs of tasks this task depends on (e.g. task_0, task_1)")
    sub_agents: List[str] = Field(default_factory=list, description="Optional list of specialized agents for this task")

class DecompositionResponse(BaseModel):
    plan_summary: str
    tasks: List[SubTask]
    confidence_score: float = Field(default=1.0)
    risk_assessment: str = Field(default="")

@dataclass
class SmartDecomposerModule:
    name: str = "smart_decomposer"
    _api: KernelAPI | None = None

    def on_load(self, api: KernelAPI) -> None:
        self._api = api
        self._api.log("info", f"[DECOMP] {self.name} loaded.")

    def on_unload(self) -> None:
        pass

    def _generate_candidate_plan(self, root_task: Task, strategy: str) -> Optional[DecompositionResponse]:
        """Generates a candidate plan based on a specific strategy."""
        reasoning = self._api.get_module("reasoning")
        if not reasoning or not getattr(reasoning, "_client", None):
            return None

        prompt = (
            f"Break down the following user request into technical atomic tasks using a {strategy} strategy: {root_task.input.description}. "
            "Output valid JSON matching the DecompositionResponse schema."
        )
        system_prompt = (
            "You are an expert technical architect. "
            f"Strategy: {strategy}. "
            "Functional means focus on features. Risk-oriented means focus on security and stability. "
            "Resource-oriented means focus on parallel execution and efficiency."
        )

        try:
            return reasoning.structured_call(
                prompt, 
                DecompositionResponse, 
                system_prompt=system_prompt,
                model="gemini-3.5-flash"
            )
        except Exception as e:
            logger.warning(f"Failed to generate {strategy} candidate plan: {e}")
            return None

    def decompose_task(self, root_task: Task) -> Optional[ExecutionPlan]:
        if not self._api or root_task.type != TaskType.PLAN:
            return None

        self._api.log("info", f"[DECOMP] Orchestrating parallel planning for task: {root_task.task_id}")

        # Parallel candidate generation (simulated here, but we can do it sequentially if reasoning is fast)
        strategies = ["functional", "risk-oriented", "resource-oriented"]
        candidates = []
        for strat in strategies:
            cand = self._generate_candidate_plan(root_task, strat)
            if cand:
                candidates.append(cand)

        if not candidates:
            return None

        # If we have multiple candidates, we should merge them or pick the best.
        # For now, let's use the reasoning module to merge them into an optimal plan.
        reasoning = self._api.get_module("reasoning")
        optimal_response = None
        if len(candidates) > 1:
            merge_prompt = (
                "I have generated several candidate plans for this task. Please synthesize them into one OPTIMAL plan that balances "
                "functionality, risk mitigation, and resource efficiency. Ensure task dependencies allow for maximum parallelism where safe.\n\n"
                f"Original Task: {root_task.input.description}\n\n"
                f"Candidate Plans: {json.dumps([c.dict() for c in candidates])}"
            )
            try:
                optimal_response = reasoning.structured_call(
                    merge_prompt,
                    DecompositionResponse,
                    system_prompt="You are a Master Architect synthesizing multiple sub-plans into a final production roadmap.",
                    model="gemini-3.5-flash"
                )
            except Exception as e:
                logger.warning(f"Merging failed, using the first candidate: {e}")
                optimal_response = candidates[0]
        else:
            optimal_response = candidates[0]

        if not optimal_response:
            return None

        return self._build_execution_plan(root_task, optimal_response)

    def _build_execution_plan(self, root_task: Task, response: DecompositionResponse) -> ExecutionPlan:
        atomic_tasks = []
        id_map = {}
        raw_dependencies: list[tuple[Task, list[str]]] = []

        for i, st in enumerate(response.tasks):
            try:
                t_type = TaskType(st.task_type.lower())
            except ValueError:
                t_type = TaskType.CODE
            
            task = Task(
                type=t_type,
                input=TaskInput(description=st.description, acceptance_criteria=[f"{st.title} completed"]),
                context=root_task.context,
                priority=Priority(st.priority.lower()) if st.priority.lower() in ["low", "normal", "high", "critical"] else Priority.NORMAL,
                parent_task_id=root_task.task_id,
                routing_hints={
                    "sub_agents": st.sub_agents,
                    "parallel_group": len(st.sub_agents) > 1,
                    "strategy_context": response.risk_assessment
                }
            )
            
            id_map[f"task_{i}"] = task.task_id
            id_map[st.title.lower().replace(" ", "_")] = task.task_id
            
            raw_dependencies.append((task, list(st.dependencies)))
            atomic_tasks.append(task)

        for task, deps in raw_dependencies:
            for dep in deps:
                dep_key = dep.strip()
                dep_id = id_map.get(dep_key)
                if dep_id and dep_id != task.task_id and dep_id not in task.dependencies:
                    task.dependencies.append(dep_id)

        self._api.log("info", f"[DECOMP] Generated optimal plan with {len(atomic_tasks)} tasks.")
        return ExecutionPlan(root_task_id=root_task.task_id, atomic_tasks=atomic_tasks)

    def before_task(self, task: Any, context: dict[str, Any]) -> None:
        pass

    def after_task(self, task: Any, result: Any, context: dict[str, Any]) -> None:
        pass

    def finalize(self) -> dict[str, Any]:
        reasoning = self._api.get_module("reasoning") if self._api else None
        ready = bool(reasoning and getattr(reasoning, "_client", None))
        return {"status": "active" if ready else "fallback_only"}
