from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, List

from .kernel_protocol import KernelAPI, KernelModule
from .models import Task, TaskType, ExecutionPlan

logger = logging.getLogger("readability_policy")

@dataclass
class CodeReadabilityModule(KernelModule):
    name: str = "readability_policy"
    _api: KernelAPI | None = None
    enabled: bool = True

    def on_load(self, api: KernelAPI) -> None:
        self._api = api
        self._api.log("info", "[READABILITY] Code Readability & Commenting Policy active.")

    def on_unload(self) -> None:
        pass

    def enforce_plan(self, plan: ExecutionPlan) -> ExecutionPlan:
        """
        Injects an explicit Commenting/Documentation task after each CODE task.
        Also mandates comments in the CODE task itself.
        """
        if not self.enabled:
            return plan

        new_tasks = []
        id_map = {t.task_id: t for t in plan.atomic_tasks}
        
        for task in plan.atomic_tasks:
            new_tasks.append(task)
            
            if task.type == TaskType.CODE:
                # 1. Mandate comments in the CODE task itself
                comment_instruction = "All functions, classes, and complex logic blocks MUST be documented with clear, human-readable comments."
                if comment_instruction not in task.input.constraints:
                    task.input.constraints.append(comment_instruction)
                if "Code is fully commented and explained" not in task.input.acceptance_criteria:
                    task.input.acceptance_criteria.append("Code is fully commented and explained")

                # 2. Inject an explicit DOCUMENTATION task for a final commenting pass
                self._api.log("info", f"[READABILITY] Injecting commenting phase for task: {task.task_id}")
                
                doc_task = Task(
                    type=TaskType.DOCS,
                    input=task.input.model_copy(),
                    context=task.context,
                    priority=task.priority,
                    parent_task_id=task.parent_task_id,
                    required_capability="docs",
                    dependencies=[task.task_id] # Depend on the code implementation
                )
                doc_task.input.description = f"Code Commenting & Documentation Pass for: {task.input.description}"
                doc_task.input.acceptance_criteria = [
                    "All exported functions have human-readable descriptions",
                    "Complexity in logic is explained with inline comments",
                    "Variable names and types are clear and documented if ambiguous"
                ]
                
                # Update subsequent tasks that depended on CODE to now depend on DOCS
                for potential_child in plan.atomic_tasks:
                    if task.task_id in potential_child.dependencies:
                        potential_child.dependencies.remove(task.task_id)
                        potential_child.dependencies.append(doc_task.task_id)
                
                new_tasks.append(doc_task)
            
        plan.atomic_tasks = new_tasks
        return plan

    def before_task(self, task: Task, context: dict[str, Any]) -> None:
        pass

    def after_task(self, task: Task, result: Any, context: dict[str, Any]) -> None:
        pass

    def finalize(self) -> dict[str, Any]:
        return {"status": "active", "policy": "mandatory_commenting"}
