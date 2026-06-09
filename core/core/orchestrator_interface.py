import asyncio
from core.core.orchestrator import Orchestrator
from core.core.models import Task, TaskType, TaskInput, TaskContext, Priority

class TaskOrchestrationInterface:
    def __init__(self):
        self.orchestrator = Orchestrator()

    def execute_complex_task(self, objective: str, project_path: str = "./"):
        """
        Executes a high-level goal using the Generative Drafting -> Parallel Execution pipeline.
        """
        # 1. Create a root task
        root_task = Task(
            type=TaskType.PLAN,
            input=TaskInput(description=objective),
            context=TaskContext(project="app", repo_path=project_path),
            priority=Priority.HIGH
        )
        
        # 2. Direct Draft Generation
        draft = self.orchestrator.decomposer.create_draft(objective)
        
        # 3. Decompose into DAG
        plan = self.orchestrator.decomposer.decompose_from_draft(root_task, draft)
        
        # 4. Enforce Policies (TDD + Readability)
        tdd = self.orchestrator.module_manager.get_module("tdd_policy")
        if tdd:
            plan = tdd.enforce_plan(plan)
            
        readability = self.orchestrator.module_manager.get_module("readability_policy")
        if readability:
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
