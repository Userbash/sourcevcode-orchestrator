from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


@dataclass(slots=True)
class KPIEventLogger:
    file_path: Path
    summary_path: Path | None = None

    @classmethod
    def from_env(cls) -> "KPIEventLogger":
        configured = (os.getenv("AI_BRIDGE_KPI_LOG_FILE") or "").strip()
        if configured:
            target = Path(configured)
        else:
            base_dir = Path((os.getenv("AI_BRIDGE_MEMORY_STORE_DIR") or "memory_store").strip())
            target = base_dir / "kpi_events.jsonl"
        summary_configured = (os.getenv("AI_BRIDGE_KPI_REJECTION_SUMMARY_PATH") or "").strip()
        summary_path = Path(summary_configured) if summary_configured else target.with_name("kpi_rejection_summary.json")
        target.parent.mkdir(parents=True, exist_ok=True)
        if summary_path:
            summary_path.parent.mkdir(parents=True, exist_ok=True)
        return cls(file_path=target, summary_path=summary_path)

    def write(self, payload: dict[str, Any]) -> None:
        row = dict(payload)
        row.setdefault("logged_at", datetime.now(UTC).isoformat())
        with self.file_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=True) + "\n")

    def write_summary(self, payload: dict[str, Any]) -> None:
        if self.summary_path is None:
            return
        row = dict(payload)
        row.setdefault("logged_at", datetime.now(UTC).isoformat())
        self.summary_path.write_text(json.dumps(row, ensure_ascii=True, indent=2, sort_keys=True), encoding="utf-8")

    def append_fallback(self, payload: dict[str, Any], *, fallback_path: Path | None = None) -> None:
        target = fallback_path or self.file_path.with_name("task_lifecycle_fallback.jsonl")
        target.parent.mkdir(parents=True, exist_ok=True)
        row = dict(payload)
        row.setdefault("logged_at", datetime.now(UTC).isoformat())
        with target.open("a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=True) + "\n")
