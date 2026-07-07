from __future__ import annotations

import argparse
import os
from dataclasses import dataclass
from typing import Any

from core.core.control_ws_client import run_control_ws_action_sync


@dataclass(slots=True)
class SmokeResult:
    ok: bool
    label: str
    transport: str
    payload: dict[str, Any]
    error_message: str | None = None


def _base_url() -> str:
    return os.getenv("AI_BRIDGE_API_URL", "http://127.0.0.1:8000").rstrip("/")


def _ws_json(base_url: str, action: str, payload: dict[str, Any] | None = None, *, frame_type: str = "command", timeout_sec: float = 10.0) -> SmokeResult:
    try:
        result = run_control_ws_action_sync(base_url, action, payload, frame_type=frame_type, timeout_sec=timeout_sec)
        terminal_payload = result.terminal_data()
        ok = result.terminal.get("type") != "error" and str(terminal_payload.get("status") or "ok") != "error"
        return SmokeResult(
            ok=ok,
            label=action,
            transport="ws",
            payload=terminal_payload,
            error_message=None if ok else str(result.terminal.get("error") or terminal_payload.get("error") or "ws request failed"),
        )
    except Exception as exc:
        return SmokeResult(False, action, "ws", {}, str(exc))


def run_smoke_check(base_url: str, timeout_sec: float = 10.0) -> int:
    requests = [
        ("sourcecraft.status.get", None, "command"),
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
        ),
    ]

    results: list[SmokeResult] = []
    for action, payload, frame_type in requests:
        results.append(_ws_json(base_url, action, payload, frame_type=frame_type, timeout_sec=timeout_sec))

    ok = True
    print("SourceCraft Smoke Check")
    print(f"Base URL: {base_url}")
    for result in results:
        if result.ok:
            print(f"[{result.label}] OK transport={result.transport}")
        else:
            print(f"[{result.label}] FAIL transport={result.transport} error={result.error_message}")
            ok = False
            continue

        if result.label == "sourcecraft.status.get":
            status = str(result.payload.get("status", "unknown"))
            role = result.payload.get("role", {}) if isinstance(result.payload.get("role"), dict) else {}
            print(f"  sourcecraft_status={status} role={role.get('name', 'unknown')}")
            if status == "error":
                ok = False
        elif result.label == "sourcecraft.delegate":
            route = result.payload.get("route", {}) if isinstance(result.payload.get("route"), dict) else {}
            schedule = result.payload.get("schedule", {}) if isinstance(result.payload.get("schedule"), dict) else {}
            delegation = result.payload.get("delegation", {}) if isinstance(result.payload.get("delegation"), dict) else {}
            print(
                f"  assigned_agent={route.get('assigned_agent', 'unknown')} route_mode={schedule.get('route_mode', 'unknown')} owner={delegation.get('recommended_owner', 'unknown')}"
            )
            if schedule.get("route_mode") == "unknown":
                ok = False

    print("RESULT: OK" if ok else "RESULT: FAIL")
    return 0 if ok else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Smoke-test SourceCraft over the orchestrator control websocket.")
    parser.add_argument("--base-url", default=_base_url(), help="Orchestrator base URL, default: %(default)s")
    parser.add_argument("--timeout-sec", type=float, default=10.0, help="Request timeout in seconds")
    args = parser.parse_args(argv)
    return run_smoke_check(args.base_url, timeout_sec=args.timeout_sec)


if __name__ == "__main__":
    raise SystemExit(main())
