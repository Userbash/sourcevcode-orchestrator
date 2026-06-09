import asyncio
from core.core.orchestrator import Orchestrator
from core.core.models import Task, TaskType, TaskInput, TaskContext

async def test_tdd_integration():
    orch = Orchestrator()
    # Create a simple CODE task
    task = Task(
        type=TaskType.PLAN, # PLAN triggers decomposition
        input=TaskInput(description="Create a hello world function"),
        context=TaskContext(project="test")
    )
    
    plan = orch.create_execution_plan(task)
    
    print(f"--- Decomposed Plan Test ---")
    print(f"Total tasks in plan: {len(plan.atomic_tasks)}")
    for i, t in enumerate(plan.atomic_tasks):
        print(f"Task {i}: {t.type.value} - {t.input.description} (Deps: {t.dependencies})")
        if "tdd_phase" in t.routing_hints:
            print(f"  -> TDD Phase: {t.routing_hints['tdd_phase']}")

    print(f"\n--- Direct run() Test ---")
    # run() handles decomposition internally
    result = orch.run(task)
    print(f"Execution status: {result['status']}")
    # Verify that the sequence of results includes the RED phase test
    for i, res in enumerate(result.get('results', [])):
        print(f"Step {i}: {res['agent_id']} - {res['status']} (Task ID: {res['task_id']})")


if __name__ == "__main__":
    asyncio.run(test_tdd_integration())
