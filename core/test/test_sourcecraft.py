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
    result = orch.dispatch(task)
    print(f"Статус задачи: {result.status.value}")
    if hasattr(result, 'output'):
        print(f"Вывод: {result.output}")
    if result.errors:
        print(f"Ошибки: {result.errors}")

if __name__ == "__main__":
    asyncio.run(main())
