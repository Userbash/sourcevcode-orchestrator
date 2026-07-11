from __future__ import annotations

import hashlib
import json
from collections import defaultdict, deque
from typing import Any


class AgentLoopGuard:
    def __init__(self, max_repeats: int = 3, signature_window: int = 6) -> None:
        self.max_repeats = max(2, int(max_repeats))
        self.signature_window = max(self.max_repeats, int(signature_window))
        self._handoff_history: dict[tuple[str, str, str], deque[str]] = defaultdict(lambda: deque(maxlen=self.signature_window))
        self._failure_history: dict[tuple[str, str], deque[str]] = defaultdict(lambda: deque(maxlen=self.signature_window))

    @staticmethod
    def _stable_signature(payload: dict[str, Any]) -> str:
        normalized = json.dumps(payload, sort_keys=True, ensure_ascii=True, separators=(",", ":"))
        return hashlib.sha256(normalized.encode("utf-8")).hexdigest()

    def should_suppress_handoff(self, *, from_agent: str, to_agent: str, task_id: str, dependency_task_id: str | None, summary: str, artifacts: list[str], errors: list[str]) -> bool:
        key = (from_agent, to_agent, task_id)
        signature = self._stable_signature(
            {
                "dependency_task_id": dependency_task_id,
                "summary": summary,
                "artifacts": list(artifacts or []),
                "errors": list(errors or []),
            }
        )
        history = self._handoff_history[key]
        history.append(signature)
        return len(history) >= self.max_repeats and len(set(list(history)[-self.max_repeats:])) == 1

    def record_result(self, *, agent_id: str, task_id: str, status: str, summary: str, errors: list[str]) -> bool:
        if status != "failed":
            self._failure_history.pop((agent_id, task_id), None)
            return False
        key = (agent_id, task_id)
        signature = self._stable_signature({"summary": summary, "errors": list(errors or [])})
        history = self._failure_history[key]
        history.append(signature)
        return len(history) >= self.max_repeats and len(set(list(history)[-self.max_repeats:])) == 1

    def snapshot(self) -> dict[str, Any]:
        return {
            "handoff_keys": len(self._handoff_history),
            "failure_keys": len(self._failure_history),
            "max_repeats": self.max_repeats,
            "signature_window": self.signature_window,
        }
