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
    logger = KPIEventLogger.from_env()
    now = datetime.now(UTC).isoformat()
    payload = {
        "event_type": "task_lifecycle",
        "task_id": f"smoke-{datetime.now(UTC).timestamp():.0f}",
        "task_type": "metrics",
        "priority": "normal",
        "status": "done",
        "agent_id": "kpi-smoke",
        "provider": "local",
        "model": "synthetic-kpi-smoke",
        "fallback_count": 0,
        "fallback_used": False,
        "started_at": now,
        "finished_at": now,
        "latency_ms": 1.0,
        "tokens_used": 1,
        "errors_count": 0,
    }
    logger.write(payload)
    logger.append_fallback(payload)
    if logger.summary_path is not None:
        logger.write_summary({
            "summary_type": "trained_memory_rejection_summary",
            "accepted": 1,
            "rejected": 0,
            "rejection_rate": 0.0,
            "by_task": {"metrics": {"accepted": 1, "rejected": 0}},
        })
    print(json.dumps({
        "status": "ok",
        "written_to": str(logger.file_path),
        "fallback_to": str(logger.file_path.with_name("task_lifecycle_fallback.jsonl")),
        "summary_to": str(logger.summary_path) if logger.summary_path else None,
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
