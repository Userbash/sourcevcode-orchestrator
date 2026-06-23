from __future__ import annotations

import asyncio

from core.adapters.orchestration.native_orchestrator_adapter import NativeOrchestratorAdapter


class _FakeOrchestrator:
    def __init__(self) -> None:
        self.async_calls: list[tuple[dict[str, object], str]] = []
        self.sync_calls: list[tuple[dict[str, object], str]] = []

    async def submit_user_task_async(self, payload, source: str = "user_input"):
        self.async_calls.append((dict(payload), source))
        return {"status": "done", "source": source}

    def submit_user_task(self, payload, source: str = "user_input"):
        self.sync_calls.append((dict(payload), source))
        return {"status": "done", "source": source}


def test_native_orchestrator_adapter_uses_async_submission_path():
    orchestrator = _FakeOrchestrator()
    adapter = NativeOrchestratorAdapter(orchestrator)

    result = asyncio.run(adapter.run({"description": "route async"}))

    assert result["status"] == "done"
    assert orchestrator.async_calls == [({"description": "route async"}, "native_adapter")]
    assert orchestrator.sync_calls == []
