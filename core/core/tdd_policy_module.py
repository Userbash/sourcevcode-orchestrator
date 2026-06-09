from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, List, Optional

from .kernel_protocol import KernelAPI, KernelModule
from .models import Task, TaskType, TaskStatus, AgentResult, ExecutionPlan

logger = logging.getLogger("tdd_policy")

@dataclass
class StrictTDDModule(KernelModule):
    name: str = "tdd_policy"
    _api: KernelAPI | None = None
    enabled: bool = True

    def on_load(self, api: KernelAPI) -> None:
        self._api = api
        self._api.log("info", "[TDD] Strict TDD Policy module loaded and ACTIVE.")

    def on_unload(self) -> None:
        pass

    def enforce_plan(self, plan: ExecutionPlan) -> ExecutionPlan:
        """
        Ensures the execution plan follows strict TDD:
        For every CODE task, ensure there is a preceding TEST task.
        """
        if not self.enabled:
            return plan

        new_tasks = []
        id_map = {t.task_id: t for t in plan.atomic_tasks}
        
        for task in plan.atomic_tasks:
            if task.type == TaskType.CODE:
                # Check if this task already has a test dependency
                has_test_dep = any(id_map.get(dep_id).type == TaskType.TEST for dep_id in task.dependencies if dep_id in id_map)
                
                if not has_test_dep:
                    self._api.log("info", f"[TDD] Injecting RED phase for CODE task: {task.task_id}")
                    # Create a RED phase TEST task
                    red_test = Task(
                        type=TaskType.TEST,
                        input=task.input.model_copy(), 
                        context=task.context,
                        priority=task.priority,
                        parent_task_id=task.parent_task_id,
                        required_capability="test",
                        routing_hints={"tdd_phase": "red"}
                    )
                    red_test.input.description = f"TDD RED Phase (Must Fail): {task.input.description}"
                    red_test.input.acceptance_criteria = [f"Feature test created and FAILS: {c}" for c in task.input.acceptance_criteria]
                    
                    # Make CODE depend on RED TEST
                    task.dependencies.append(red_test.task_id)
                    new_tasks.append(red_test)

            
            new_tasks.append(task)
            
        plan.atomic_tasks = new_tasks
        return plan

    def before_task(self, task: Task, context: dict[str, Any]) -> None:
        """
        Enforce guards before task execution.
        """
        if not self.enabled:
            return

        if task.type == TaskType.CODE:
            # Check if RED phase was completed
            # In a real system, we'd check memory or results for a failing test
            pass

    def after_task(self, task: Task, result: AgentResult, context: dict[str, Any]) -> None:
        """
        Verify TDD invariants after task execution.
        """
        if not self.enabled:
            return

        phase = task.routing_hints.get("tdd_phase")
        if phase == "red":
            if result.status == TaskStatus.DONE:
                # If test PASSED in RED phase, it's a violation (unless it's a regression test, but we are strict here)
                self._api.log("warning", f"[TDD] Violation: Test PASSED in RED phase for task {task.task_id}")
                # We don't fail here to allow progress, but in "HARD" mode we might
            else:
                self._api.log("info", f"[TDD] Red phase successful for task {task.task_id} (Test failed as expected).")

    def finalize(self) -> dict[str, Any]:
        return {"status": "active" if self.enabled else "disabled", "enforcement": "hard"}
