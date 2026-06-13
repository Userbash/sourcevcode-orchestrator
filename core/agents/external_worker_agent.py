from __future__ import annotations
import json
import redis
import os
from core.agents.base_agent import BaseAgent
from core.core.models import AgentResult, Task, TaskStatus, ResultOutput

class ExternalWorkerAgent(BaseAgent):
    def __init__(self, agent_id: str, capabilities: list[str], queue_name: str) -> None:
        super().__init__(agent_id, capabilities)
        self.queue_name = queue_name
        self.redis_url = os.getenv("REDIS_URL", "redis://localhost:6379")
        self._redis = None

    @property
    def client(self):
        if self._redis is None:
            self._redis = redis.from_url(self.redis_url)
        return self._redis

    def run(self, task: Task, memory_context: dict | None = None) -> AgentResult:
        payload = {
            "task_id": task.task_id,
            "agent_id": self.agent_id,
            "input": task.input.as_dict(),
            "context": task.context.as_dict(),
            "memory": memory_context or {}
        }
        
        # Push task to Redis queue
        self.client.rpush(f"queue:{self.queue_name}", json.dumps(payload))
        
        # Wait for result (simple blocking pop for demonstration, in production use event bus)
        # Note: This is simplified. In a real system, we'd use the MessageBus logic.
        result_key = f"result:{task.task_id}"
        _, result_data = self.client.blpop(result_key, timeout=300) # 5 min timeout
        
        if not result_data:
            return self.result(task, "Worker timeout", status=TaskStatus.FAILED, errors=["Worker did not respond in time"])
            
        data = json.loads(result_data)
        return AgentResult(
            task_id=task.task_id,
            agent_id=self.agent_id,
            status=TaskStatus(data["status"]),
            output=ResultOutput(**data.get("output", {})),
            confidence=data.get("confidence", 0.9),
            errors=data.get("errors", []),
            next_recommendations=list(data.get("next_recommendations", [])),
            provider=data.get("provider"),
            model_name=data.get("model_name"),
        )
