import asyncio
from core.core.orchestrator import Orchestrator
from core.core.models import Task, TaskType, TaskInput, TaskContext, Priority

class TaskOrchestrationInterface:
    def __init__(self, verbose_orchestrator: bool = False, json_console: bool = False):
        self.orchestrator = Orchestrator(verbose_orchestrator=verbose_orchestrator, json_console=json_console)

    def execute_complex_task(self, objective: str, project_path: str = "./", verbose_orchestrator: bool | None = None, json_console: bool | None = None):
        """
        Executes a high-level goal using the Generative Drafting -> Parallel Execution pipeline.
        """
        if verbose_orchestrator is not None:
            self.orchestrator.verbose_orchestrator = verbose_orchestrator
            self.orchestrator.console.set_mode(verbose=verbose_orchestrator)
        if json_console is not None:
            self.orchestrator.json_console = json_console
            self.orchestrator.console.set_mode(json_mode=json_console)

        # 1. Create a root task
        self.orchestrator.console.emit("PLAN", f"Подготовка задачи: {objective[:120]}")
        root_task = Task(
            type=TaskType.PLAN,
            input=TaskInput(description=objective),
            context=TaskContext(project="app", repo_path=project_path),
            priority=Priority.HIGH
        )

        # 2. Direct Draft Generation
        self.orchestrator.console.emit("PLAN", "Генерация черновика плана")
        draft = self.orchestrator.decomposer.create_draft(objective)

        # 3. Decompose into DAG
        self.orchestrator.console.emit("PLAN", "Декомпозиция в DAG и параллельные ветки")
        plan = self.orchestrator.decomposer.decompose_from_draft(root_task, draft)

        # 4. Enforce Policies (TDD + Readability)
        tdd = self.orchestrator.module_manager.get_module("tdd_policy")
        if tdd:
            self.orchestrator.console.emit("PLAN", "Применение TDD policy")
            plan = tdd.enforce_plan(plan)

        readability = self.orchestrator.module_manager.get_module("readability_policy")
        if readability:
            self.orchestrator.console.emit("PLAN", "Применение readability policy")
            plan = readability.enforce_plan(plan)

        # 5. Execute in Parallel
        self.orchestrator.console.emit("START", "Запуск мульти-агентной параллельной оркестрации")
        try:
            # We must use asyncio.run to execute the async method if called synchronously
            result = asyncio.run(self.orchestrator.run_plan_parallel(plan))
            return result
        except Exception as e:
            self.orchestrator.console.emit("ERROR", f"Сбой параллельного выполнения: {e}")
            return {"status": "failed", "error": str(e)}
