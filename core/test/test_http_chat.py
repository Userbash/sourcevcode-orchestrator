from __future__ import annotations

import pytest

from core.test.ws_test_helper import compact_frame, run_ws_request


def test_ws_chat():
    if not __import__("os").getenv("RUN_LIVE_CHAT_TESTS") == "1":
        pytest.skip("live orchestrator not enabled")
    data = run_ws_request("ws://localhost:8000/chat/ws", compact_frame(user_id="test_user", session_id="test_session", message="tell me a 5 word joke", source="cli_test", provider="test"), open_timeout=30, recv_timeout=60)
    assert data["type"] == "final_result"
    assert data["status"] in {"completed", "done", "ok"}
