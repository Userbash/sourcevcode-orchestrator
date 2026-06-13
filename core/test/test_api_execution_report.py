from __future__ import annotations

import pytest

from core.test.ws_test_helper import compact_frame, run_ws_request


def test_api():
    if not __import__("os").getenv("RUN_LIVE_CHAT_TESTS") == "1":
        pytest.skip("live orchestrator not enabled")
    data = run_ws_request("ws://localhost:8000/chat/ws", compact_frame(user_id="test_script", session_id="test-session-1", message="RESEARCH: test integration format", source="test_script", provider="test"), open_timeout=30, recv_timeout=60)
    result = data.get("result", {})
    summary = result.get("summary", "") if isinstance(result, dict) else str(result)
    assert "AI ORCHESTRATOR EXECUTION REPORT" in summary
