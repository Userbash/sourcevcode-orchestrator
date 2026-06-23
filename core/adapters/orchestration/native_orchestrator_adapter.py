from __future__ import annotations

from core.core.orchestrator import Orchestrator


class NativeOrchestratorAdapter:
    def __init__(self, orchestrator: Orchestrator) -> None:
        self.orchestrator = orchestrator

    async def run(self, payload: dict[str, object]) -> dict[str, object]:
        return await self.orchestrator.submit_user_task_async(payload, source="native_adapter")

    def run_sync(self, payload: dict[str, object]) -> dict[str, object]:
        return self.orchestrator.submit_user_task(payload, source="native_adapter")
