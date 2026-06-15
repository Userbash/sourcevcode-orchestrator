import asyncio
from core.core.orchestrator import Orchestrator
from core.core.models import Task, TaskType, Priority, TaskInput, TaskContext

async def main():
    orch = Orchestrator()
    task = Task(
        task_id="test-sc-1",
        type=TaskType.RESEARCH,
        priority=Priority.NORMAL,
        input=TaskInput(description="Summarize repository state and check working tree status. What branch are we on?"),
        context=TaskContext(project="wisper", repo_path=".", branch="main"),
        required_capability="sourcecraft"
    )
    
    print("Отправка задачи в Оркестратор...")
    result = await orch.run(task)
    print(f"Статус задачи: {result.get('status')}")
    merged = result.get('merged')
    if merged:
        if isinstance(merged, dict):
            print(f"Вывод: {merged.get('summary')}")
            if merged.get('errors'):
                print(f"Ошибки: {merged.get('errors')}")
        else:
            print(f"Вывод: {getattr(merged, 'summary', '')}")
            if getattr(merged, 'errors', None):
                print(f"Ошибки: {getattr(merged, 'errors', None)}")

if __name__ == "__main__":
    asyncio.run(main())
