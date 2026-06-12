#!/usr/bin/env python3
from __future__ import annotations

import json
import sys
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.core.kpi_event_logger import KPIEventLogger


def main() -> None:
    logger = KPIEventLogger(file_path=Path("memory_store/kpi_events.jsonl"), summary_path=Path("memory_store/kpi_rejection_summary.json"))
    payload = {
        "event_type": "task_lifecycle",
        "task_id": f"smoke-{datetime.now(UTC).timestamp():.0f}",
        "task_type": "plan",
        "priority": "normal",
        "status": "done",
        "agent_id": "smoke-agent",
        "provider": "local",
        "model": "synthetic-smoke",
        "fallback_count": 0,
        "fallback_used": False,
        "started_at": datetime.now(UTC).isoformat(),
        "finished_at": datetime.now(UTC).isoformat(),
        "latency_ms": 1.0,
        "tokens_used": 1,
        "errors_count": 0,
    }
    logger.write(payload)
    logger.append_fallback(payload)
    print(json.dumps({"status": "ok", "written_to": str(logger.file_path), "fallback_to": str(logger.file_path.with_name("task_lifecycle_fallback.jsonl"))}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
