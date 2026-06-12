from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
import json
from pathlib import Path
from typing import Any

from .models import AgentRecord, Task


@dataclass(slots=True)
class UserConsole:
    events: list[str] = field(default_factory=list)
    listeners: list[Any] = field(default_factory=list)
    json_mode: bool = False
    verbose: bool = False
    color_mode: bool = False
    log_path: Path | None = None
    _started_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    _last_progress_key: tuple[str, int, int] | None = None

    def set_mode(
        self,
        *,
        json_mode: bool | None = None,
        verbose: bool | None = None,
        color_mode: bool | None = None,
        log_path: str | Path | None = None,
    ) -> None:
        if json_mode is not None:
            self.json_mode = json_mode
        if verbose is not None:
            self.verbose = verbose
        if color_mode is not None:
            self.color_mode = color_mode
        if log_path is not None:
            self.log_path = Path(log_path)

    def _now_payload(self, stage: str, message: str) -> dict[str, Any]:
        now = datetime.now(UTC)
        elapsed = (now - self._started_at).total_seconds()
        return {
            "ts": now.isoformat(),
            "elapsed_s": round(elapsed, 3),
            "stage": stage,
            "message": message,
        }

    def _ansi(self, code: str, text: str) -> str:
        if not self.color_mode:
            return text
        return f"\033[{code}m{text}\033[0m"

    def _stage_color(self, stage: str) -> str:
        palette = {
            "ERROR": "31",
            "WARN": "33",
            "WARNING": "33",
            "DONE": "32",
            "START": "36",
            "PLAN": "35",
            "PARALLEL": "34",
            "PROGRESS": "92",
            "TASK START": "36",
            "TASK END": "32",
            "THROTTLE": "33",
        }
        return palette.get(stage, "37")

    def _format_human(self, payload: dict[str, Any]) -> str:
        stage = str(payload["stage"])
        ts = payload["ts"]
        elapsed = payload["elapsed_s"]
        message = payload["message"]
        stage_label = self._ansi(self._stage_color(stage), f"[{stage}]")
        return f"[{ts}] +{elapsed:.3f}s {stage_label} {message}"

    def _append_file(self, payload: dict[str, Any]) -> None:
        if self.log_path is None:
            return
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        with self.log_path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(payload, ensure_ascii=False) + "\n")

    def emit(self, stage: str, message: str) -> None:
        payload = self._now_payload(stage, message)
        line = json.dumps(payload, ensure_ascii=False) if self.json_mode else self._format_human(payload)
        self.events.append(line)
        print(line)
        self._append_file(payload)
        for listener in self.listeners:
            try:
                listener(stage, message)
            except Exception:
                pass

    def progress(self, label: str, current: int, total: int, *, details: str = "") -> None:
        total = max(total, 0)
        current = max(0, min(current, total if total else current))
        self._last_progress_key = (label, current, total)
        width = 24
        ratio = 1.0 if total == 0 else current / total
        filled = min(width, int(round(ratio * width)))
        bar = "█" * filled + "░" * (width - filled)
        percent = 100 if total == 0 else int(round(ratio * 100))
        message = f"{label} |{bar}| {current}/{total} ({percent}%)"
        if details:
            message = f"{message} - {details}"
        self.emit("PROGRESS", message)

    def agent_status(self, agent: AgentRecord, task: Task | None = None, progress: int = 0, stage: str = "idle") -> str:
        task_text = task.input.description if task else "нет активной задачи"
        line = (
            f"Агент: {agent.id}\n"
            f"Статус: {agent.status.value}\n"
            f"Задача: {task_text}\n"
            f"Модель: {agent.metrics.model_name or getattr(agent, 'model_name', None) or 'unknown'}\n"
            f"Прогресс: {progress}%\n"
            f"Текущий этап: {stage}\n"
            f"Ошибки: {agent.disabled_reason or 'нет'}"
        )
        self.events.append(line)
        return line
