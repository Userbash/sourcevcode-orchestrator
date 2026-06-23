from __future__ import annotations

import asyncio
import json

from core.core.task_listener import TaskListener


class _FakeOrchestrator:
    def __init__(self) -> None:
        self.calls: list[tuple[dict[str, object], str]] = []

    async def submit_user_task_async(self, payload, source: str = "user_input"):
        assert isinstance(payload, dict)
        self.calls.append((dict(payload), source))
        return {"task_id": str(payload.get("task_id") or ""), "status": "done", "source": source}


def test_task_listener_uses_async_submission_and_stable_task_id(tmp_path):
    orchestrator = _FakeOrchestrator()
    listener = TaskListener(orchestrator, result_dir=str(tmp_path))

    asyncio.run(listener._process_payload({"description": "fix async queue path", "type": "code"}))

    assert len(orchestrator.calls) == 1
    payload, source = orchestrator.calls[0]
    assert source == "queue"
    task_id = str(payload.get("task_id") or "")
    assert task_id

    result_path = tmp_path / f"{task_id}.json"
    assert result_path.exists()
    written = json.loads(result_path.read_text(encoding="utf-8"))
    assert written["task_id"] == task_id
    assert written["status"] == "done"


def test_task_listener_submit_user_input_uses_async_path(tmp_path):
    orchestrator = _FakeOrchestrator()
    listener = TaskListener(orchestrator, result_dir=str(tmp_path))

    result = asyncio.run(listener.submit_user_input("fix direct async input"))

    assert result["status"] == "done"
    assert len(orchestrator.calls) == 1
    task_id = str(orchestrator.calls[0][0].get("task_id") or "")
    assert (tmp_path / f"{task_id}.json").exists()
