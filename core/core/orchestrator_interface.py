from __future__ import annotations

import asyncio

from core.core.models import ExecutionPlan, Priority, Task, TaskContext, TaskInput, TaskType
from core.core.orchestrator import Orchestrator


class TaskOrchestrationInterface:
    def __init__(self, verbose_orchestrator: bool = False, json_console: bool = False) -> None:
        self.orchestrator = Orchestrator(verbose_orchestrator=verbose_orchestrator, json_console=json_console)

    def _apply_console_overrides(
        self,
        *,
        verbose_orchestrator: bool | None = None,
        json_console: bool | None = None,
    ) -> None:
        if verbose_orchestrator is not None:
            self.orchestrator.verbose_orchestrator = verbose_orchestrator
            self.orchestrator.console.set_mode(verbose=verbose_orchestrator)
        if json_console is not None:
            self.orchestrator.json_console = json_console
            self.orchestrator.console.set_mode(json_mode=json_console)

    def _build_execution_plan(self, objective: str, project_path: str) -> ExecutionPlan:
        self.orchestrator.console.emit("PLAN", f"Подготовка задачи: {objective[:120]}")
        root_task = Task(
            type=TaskType.PLAN,
            input=TaskInput(description=objective),
            context=TaskContext(project="app", repo_path=project_path),
            priority=Priority.HIGH,
        )

        self.orchestrator.console.emit("PLAN", "Генерация черновика плана")
        draft = self.orchestrator.decomposer.create_draft(objective)

        self.orchestrator.console.emit("PLAN", "Декомпозиция в DAG и параллельные ветки")
        plan = self.orchestrator.decomposer.decompose_from_draft(root_task, draft)

        tdd = self.orchestrator.module_manager.get_module("tdd_policy")
        if tdd:
            self.orchestrator.console.emit("PLAN", "Применение TDD policy")
            plan = tdd.enforce_plan(plan)

        readability = self.orchestrator.module_manager.get_module("readability_policy")
        if readability:
            self.orchestrator.console.emit("PLAN", "Применение readability policy")
            plan = readability.enforce_plan(plan)

        return plan

    async def execute_complex_task_async(
        self,
        objective: str,
        project_path: str = "./",
        verbose_orchestrator: bool | None = None,
        json_console: bool | None = None,
    ) -> dict[str, object]:
        self._apply_console_overrides(
            verbose_orchestrator=verbose_orchestrator,
            json_console=json_console,
        )
        plan = self._build_execution_plan(objective, project_path)
        self.orchestrator.console.emit("START", "Запуск мульти-агентной параллельной оркестрации")
        return await self.orchestrator.run_plan_parallel(plan)

    def execute_complex_task(
        self,
        objective: str,
        project_path: str = "./",
        verbose_orchestrator: bool | None = None,
        json_console: bool | None = None,
    ) -> dict[str, object]:
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return asyncio.run(
                self.execute_complex_task_async(
                    objective,
                    project_path=project_path,
                    verbose_orchestrator=verbose_orchestrator,
                    json_console=json_console,
                )
            )

        raise RuntimeError(
            "execute_complex_task() cannot be called from a running event loop; use await execute_complex_task_async()"
        )
