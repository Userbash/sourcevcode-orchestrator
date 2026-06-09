import asyncio
import os
from core.core.orchestrator import Orchestrator
from core.core.models import Task, TaskType, TaskInput, TaskContext

async def test_readability_injection():
    # Disable autostart to avoid pulling models during test
    os.environ["AI_BRIDGE_AUTOSTART_LOCAL_LLM"] = "false"
    os.environ["AI_BRIDGE_AUTO_BOOTSTRAP"] = "false"
    
    orch = Orchestrator()
    task = Task(
        type=TaskType.PLAN,
        input=TaskInput(description="Create a user profile component"),
        context=TaskContext(project="test")
    )
    
    plan = orch.create_execution_plan(task)
    
    print(f"Total tasks in plan: {len(plan.atomic_tasks)}")
    for i, t in enumerate(plan.atomic_tasks):
        print(f"Task {i}: {t.type.value} - {t.input.description} (Deps: {t.dependencies})")
        if t.type == TaskType.CODE:
            print(f"  -> Constraints: {t.input.constraints}")
            print(f"  -> Acceptance: {t.input.acceptance_criteria}")

if __name__ == "__main__":
    asyncio.run(test_readability_injection())
