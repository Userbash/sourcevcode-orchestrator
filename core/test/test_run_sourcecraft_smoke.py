from __future__ import annotations

from core.scripts.run_sourcecraft_smoke import run_smoke_check


class _FakeResult:
    def __init__(self, payload: dict[str, object]) -> None:
        self.terminal = {"type": "response", "error": None}
        self._payload = payload

    def terminal_data(self) -> dict[str, object]:
        return dict(self._payload)


def test_run_smoke_check_uses_control_ws_actions(monkeypatch):
    calls: list[tuple[str, dict[str, object] | None, str, float]] = []

    def _fake_run_control_ws_action_sync(base_url: str, action: str, payload: dict[str, object] | None = None, *, frame_type: str = "command", timeout_sec: float = 10.0):
        calls.append((action, payload, frame_type, timeout_sec))
        if action == "sourcecraft.status.get":
            return _FakeResult({"status": "ok", "role": {"name": "sourcecraft"}})
        if action == "sourcecraft.delegate":
            return _FakeResult(
                {
                    "status": "ok",
                    "route": {"assigned_agent": "orchestrator"},
                    "schedule": {"route_mode": "orchestrator"},
                    "delegation": {"recommended_owner": "sourcecraft"},
                }
            )
        raise AssertionError(f"unexpected action: {action}")

    monkeypatch.setattr("core.scripts.run_sourcecraft_smoke.run_control_ws_action_sync", _fake_run_control_ws_action_sync)

    exit_code = run_smoke_check("http://127.0.0.1:8000", timeout_sec=12.0)

    assert exit_code == 0
    assert calls == [
        ("sourcecraft.status.get", None, "command", 12.0),
        (
            "sourcecraft.delegate",
            {
                "description": "Prepare SourceCraft release notes and repo status",
                "task_type": "plan",
                "priority": "normal",
                "repo_path": ".",
                "branch": "main",
                "files": [],
                "constraints": ["smoke-check"],
                "acceptance_criteria": ["SourceCraft delegation succeeds"],
                "required_capability": "sourcecraft",
            },
            "command",
            12.0,
        ),
    ]
