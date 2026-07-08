from __future__ import annotations

import asyncio
import time

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


class _SessionMemoryStub:
    def get(self, *_args, **_kwargs):
        return None

    def set(self, *_args, **_kwargs):
        return None


class _ModuleManagerStub:
    def get_module(self, _name: str):
        return None


async def _submit_user_task_async_without_blocking_loop() -> dict[str, object]:
    class SubmitShim:
        console = UserConsole()
        module_manager = _ModuleManagerStub()
        session_memory = _SessionMemoryStub()
        _latest_frame_orchestrator = None
        _latest_frame_xml_package = None

        def _prepare_ingress_payload(self, normalized: dict[str, object], *, source: str = "user_input") -> dict[str, object]:
            return normalized

        def _apply_ingress_contract(self, task, normalized: dict[str, object], *, source: str = "user_input") -> None:
            return None

        def _control_module(self):
            return None

        def _memory_control_module(self):
            return None

        def run_sync(self, _task):
            time.sleep(0.05)
            return {"status": "done", "summary": "threaded execution"}

    resumed = asyncio.Event()

    async def ticker() -> None:
        await asyncio.sleep(0.01)
        resumed.set()

    submit_task = asyncio.create_task(Orchestrator.submit_user_task_async(SubmitShim(), {"message": "hello"}))
    ticker_task = asyncio.create_task(ticker())

    await asyncio.wait_for(resumed.wait(), timeout=0.03)
    result = await submit_task
    await ticker_task
    return result


def test_submit_user_task_async_offloads_run_sync_from_event_loop():
    result = asyncio.run(_submit_user_task_async_without_blocking_loop())

    assert result["status"] == "done"
    assert result["summary"] == "threaded execution"
