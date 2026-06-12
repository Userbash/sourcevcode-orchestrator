
from __future__ import annotations

import asyncio
from types import SimpleNamespace

from core.core import api_bridge_module as api_module
from core.core.api_bridge_module import APIBridgeModule, ChatRequest
from core.core.task_submission_api import create_standard_task as real_create_standard_task
from core.test.ws_test_helper import run_ws_frames


def _fake_standard_task(data):
    task = real_create_standard_task(data)
    task.task_id = "trace-task-123"
    return task


class _Control:
    def __init__(self) -> None:
        self.submissions: dict[str, dict[str, object]] = {}

    def task_status(self, task_id: str):
        return self.submissions.get(task_id)


class _Decision:
    task_id = "trace-task-123"

    def as_dict(self):
        return {"task_id": self.task_id, "route_mode": "orchestrator", "requires_orchestrator": True}


class _API:
    def __init__(self) -> None:
        self.control = _Control()
        self.live_trace_rows = []
        self.scheduler = SimpleNamespace(decisions=[_Decision()])

    def get_module(self, name):
        if name == "orchestrator_control":
            return self.control
        return None

    def submit_user_task(self, payload, source="user_input"):
        self.control.submissions["trace-task-123"] = {
            "task_id": "trace-task-123",
            "source": source,
            "description": payload["message"],
            "status": "done",
            "agent_id": "mistral-1",
            "orchestrator_source_of_truth": True,
        }
        self.live_trace_rows.append({"task_id": "trace-task-123", "router_agent": "mistral-1", "router_provider": "mistral"})
        return {"task_id": "trace-task-123", "status": "done", "results": [], "merged": {"summary": "ok"}}


def test_chat_websocket_trace_payload(monkeypatch):
    monkeypatch.setattr(api_module, "create_standard_task", _fake_standard_task)

    module = APIBridgeModule()
    module._api = _API()

    response = asyncio.run(module._chat_payload(
        ChatRequest(user_id="u1", message="trace me", session_id="sess-1", source="web_chat", provider="auto", trace=True),
        source_label="web_chat",
        provider_label="auto",
    ))

    assert response["status"] == "completed"
    assert response["delivery"]["transport"] == "websocket"
    assert response["delivery"]["endpoint"] == "/chat/ws"
    assert response["delivery"]["orchestrator"] == "submit_user_task"
    assert response["delivery"]["visibility"] == "full"
    assert response["tdd"]["status"] == "inactive"
    assert response["tdd"]["enforcement"] == "unknown"
    assert response["trace"]["input"]["message"] == "trace me"
    assert response["trace"]["normalized"]["message"] == "trace me"
    assert response["trace"]["raw_result"]["task_id"] == "trace-task-123"
    assert response["result"]["summary"].endswith("ok")


def test_chat_websocket_frames_emit_separate_antigravity_status(monkeypatch):
    module = APIBridgeModule()
    module._api = _API()
    monkeypatch.setattr(module, '_antigravity_snapshot', lambda: {'status': 'ready', 'ready': True})

    frames = module._chat_ws_frames(
        ChatRequest(user_id='u1', message='trace me', session_id='sess-1', source='web_chat', provider='auto', trace=True),
        {'status': 'completed', 'delivery': {'transport': 'websocket'}, 'trace': {'input': {'message': 'trace me'}}},
    )

    assert frames[0]['type'] == 'antigravity_status'
    assert frames[0]['antigravity_status']['ready'] is True
    assert frames[1]['type'] == 'final_result'
    assert 'antigravity_status' not in frames[1]


def test_chat_ws_stream_emits_status_then_final_result(monkeypatch):
    module = APIBridgeModule()
    module._api = _API()
    monkeypatch.setattr(module, '_antigravity_snapshot', lambda: {'status': 'ready', 'ready': True})

    frames = module._chat_ws_frames(
        ChatRequest(user_id='u1', message='trace me', session_id='sess-1', source='web_chat', provider='auto', trace=True),
        {'status': 'completed', 'delivery': {'transport': 'websocket', 'endpoint': '/chat/ws'}, 'trace': {}},
    )

    assert [frame['type'] for frame in frames] == ['antigravity_status', 'final_result']
    assert frames[0]['antigravity_status']['ready'] is True
    assert 'antigravity_status' not in frames[1]


def test_ws_helper_collects_stream_frames(monkeypatch):
    frames = [
        {"type": "antigravity_status", "antigravity_status": {"ready": True}},
        {"type": "final_result", "status": "completed"},
    ]
    assert [frame["type"] for frame in frames] == ["antigravity_status", "final_result"]
