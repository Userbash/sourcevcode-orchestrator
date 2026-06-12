from __future__ import annotations

import json
from collections import Counter, defaultdict
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any


def _parse_ts(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except Exception:
        return None


def _read_rows(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    if not path.exists():
        return rows
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except Exception:
            continue
        if isinstance(row, dict):
            rows.append(row)
    return rows


def validate_kpi(*, kpi_log_path: Path, fallback_path: Path | None = None, days: int = 7) -> dict[str, Any]:
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(days=days)
    rows = [r for r in _read_rows(kpi_log_path) if (ts := _parse_ts(r.get("started_at") or r.get("logged_at"))) and ts >= cutoff]
    task_rows = [r for r in rows if r.get("event_type") == "task_lifecycle"]
    by_day: dict[str, dict[str, Any]] = {}
    for row in task_rows:
        ts = _parse_ts(row.get("started_at") or row.get("logged_at"))
        if not ts:
            continue
        day = ts.date().isoformat()
        bucket = by_day.setdefault(day, {"tasks_total": 0, "done": 0, "failed": 0})
        bucket["tasks_total"] += 1
        bucket["done"] += 1 if row.get("status") == "done" else 0
        bucket["failed"] += 1 if row.get("status") == "failed" else 0
    missing_days = []
    for i in range(days):
        day = (now.date() - timedelta(days=i)).isoformat()
        if day not in by_day:
            missing_days.append(day)
    by_type = Counter(str(r.get("event_type") or r.get("type") or "unknown") for r in rows)
    anomalies = []
    if by_type.get("task_lifecycle", 0) == 0:
        anomalies.append("no_task_lifecycle_events")
    if by_type.get("postgres_watchdog", 0) > by_type.get("task_lifecycle", 0) * 3 and by_type.get("task_lifecycle", 0) < 5:
        anomalies.append("watchdog_overwhelms_task_telemetry")
    fallback_events = 0
    if fallback_path and fallback_path.exists():
        fallback_events = sum(1 for line in fallback_path.read_text(encoding="utf-8").splitlines() if '"event_type": "task_lifecycle"' in line)
    return {
        "window_days": days,
        "scanned_events": len(rows),
        "task_lifecycle_events": len(task_rows),
        "event_types": by_type.most_common(10),
        "daily_task_summary": by_day,
        "missing_days": missing_days,
        "fallback_task_lifecycle_events": fallback_events,
        "anomalies": anomalies,
    }
