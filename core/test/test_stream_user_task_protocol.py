from __future__ import annotations

import asyncio

from core.core.orchestrator import Orchestrator
from core.core.user_console import UserConsole


class DummyOrchestrator:
    def __init__(self, *, fail: bool = False) -> None:
        self.console = UserConsole()
        self.fail = fail

    async def submit_user_task_async(self, payload: object, source: str = "user_input"):
        self.console.emit("PLAN", "Декомпозиция в параллельные ветки")
        self.console.emit("EXECUTION", "task_id=t-1 agent=local-llm-1 touched VFS cache")
        await asyncio.sleep(0)
        if self.fail:
            raise RuntimeError("ws pipeline failed")
        return {
            "status": "done",
            "summary": "final answer body",
            "merged": {"summary": "final answer body"},
            "results": [{"agent_id": "local-llm-1", "status": "done"}],
        }


async def _collect(orchestrator: DummyOrchestrator) -> list[dict[str, object]]:
    frames: list[dict[str, object]] = []
    async for frame in Orchestrator.stream_user_task(orchestrator, {"message": "hello"}):
        frames.append(frame)
    return frames


def test_stream_user_task_emits_reasoning_protocol_frames_before_final_result():
    frames = asyncio.run(_collect(DummyOrchestrator()))

    protocol_events = [frame for frame in frames if frame.get("type") == "protocol_event"]
    raw_events = [frame for frame in frames if frame.get("type") == "stream_event"]

    assert raw_events[0]["stage"] == "ACCEPTED"
    assert protocol_events[0]["event"] == "thinking_start"
    assert any(frame["event"] == "thinking_chunk" for frame in protocol_events)
    assert any(frame["event"] == "metadata_update" for frame in protocol_events)
    assert any(frame["event"] == "thinking_end" for frame in protocol_events)
    assert any(frame["event"] == "answer_chunk" for frame in protocol_events)
    assert frames[-1]["type"] == "final_result"
    assert frames[-1]["status"] == "done"


def test_stream_user_task_emits_protocol_end_even_on_error():
    frames = asyncio.run(_collect(DummyOrchestrator(fail=True)))

    protocol_events = [frame for frame in frames if frame.get("type") == "protocol_event"]

    assert protocol_events[0]["event"] == "thinking_start"
    assert protocol_events[-1]["event"] == "thinking_end"
    assert protocol_events[-1]["payload"]["metadata"]["status"] == "error"
    assert frames[-1]["type"] == "final_result"
    assert frames[-1]["status"] == "error"
