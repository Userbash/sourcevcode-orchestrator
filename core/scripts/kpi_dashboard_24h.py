#!/usr/bin/env python3
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.core.effectiveness_dashboard import build_kpi_dashboard


def main() -> None:
    kpi_log = Path("memory_store/kpi_events.jsonl")
    rolling = Path("core/mimo/profiles/rolling_kpi_store.json")
    summary = Path("memory_store/kpi_dashboard_24h.json")
    dashboard = build_kpi_dashboard(kpi_log_path=kpi_log, rolling_kpi_path=rolling, summary_path=summary)
    print(json.dumps(dashboard, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
