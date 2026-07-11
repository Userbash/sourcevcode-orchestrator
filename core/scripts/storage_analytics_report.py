from __future__ import annotations

import json
import os
from pathlib import Path

from core.core.data_storage_analytics import (
    build_data_storage_analytics_report,
    write_data_storage_analytics_report,
)


def main() -> int:
    storage_dir = Path(os.getenv("AI_BRIDGE_MEMORY_STORE_DIR", "memory_store"))
    output_path = Path(
        os.getenv(
            "AI_BRIDGE_STORAGE_ANALYTICS_PATH",
            str(storage_dir / "data_storage_analytics.json"),
        )
    )
    database_url = os.getenv("AI_BRIDGE_MEMORY_DATABASE_URL", "").strip() or None

    report = build_data_storage_analytics_report(
        database_url=database_url,
        storage_dir=storage_dir,
    )
    write_data_storage_analytics_report(report, output_path=output_path)
    print(json.dumps(report.as_dict(), ensure_ascii=True, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
