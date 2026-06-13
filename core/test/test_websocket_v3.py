from __future__ import annotations

from core.test.ws_test_helper import compact_frame, run_ws_request


def test_ws():
    data = run_ws_request("ws://localhost:8000/chat/ws", compact_frame(user_id="test_user", session_id="test_session", message="ping status", source="cli_test", provider="test"), open_timeout=30, recv_timeout=60)
    assert data["type"] == "final_result"
    assert data["status"] in {"completed", "done", "ok"}
