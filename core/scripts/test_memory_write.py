import asyncio
from core.core.orchestrator import Orchestrator
from core.core.models import Task, TaskType, Priority
from core.core.persistent_memory import AI_BRIDGE_SCHEMA, normalize_database_url
import psycopg2
import os

async def test_write():
    # Setup Orchestrator
    orchestrator = Orchestrator()
    
    # Trigger a task
    from core.core.models import TaskType, Priority, TaskContext
    task = Task(
        task_id="test_persistence_123",
        type=TaskType.RESEARCH,
        priority=Priority.NORMAL,
        input={"description": "System status check", "message": "System status check"},
        context=TaskContext(project="test-proj", repo_path=".", branch="main"),
        memory_keys=["status"]
    )
    
    print("Running task...")
    result = await orchestrator.run(task)
    print(f"Task status: {result['status']}")
    
    # Verify DB
    dsn = normalize_database_url(os.getenv("AI_BRIDGE_MEMORY_DATABASE_URL", ""))
    with psycopg2.connect(dsn) as conn:
        with conn.cursor() as cur:
            cur.execute(f"SELECT count(*) FROM {AI_BRIDGE_SCHEMA}.memories")
            print(f"Memories in DB: {cur.fetchone()[0]}")

if __name__ == "__main__":
    asyncio.run(test_write())
