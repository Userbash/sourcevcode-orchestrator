#!/usr/bin/env python3
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.core.kpi_validation import validate_kpi


def main() -> None:
    kpi_log = Path("memory_store/kpi_events.jsonl")
    fallback = Path("memory_store/task_lifecycle_fallback.jsonl")
    report = validate_kpi(kpi_log_path=kpi_log, fallback_path=fallback, days=7)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    if report.get("missing_days") or report.get("anomalies"):
        raise SystemExit(2)


if __name__ == "__main__":
    main()
