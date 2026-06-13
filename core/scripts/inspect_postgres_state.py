from __future__ import annotations

import json
import os

from core.core.data_plane_monitor import (
    build_data_plane_snapshot,
    fetch_recent_rows,
    postgres_read_write_probe,
)


def _print_table(label: str, rows: list[dict[str, object]]) -> None:
    print(f"\n--- {label} ---")
    if not rows:
        print("(empty)")
        return
    for row in rows:
        print(json.dumps(row, ensure_ascii=True, default=str))


def main() -> int:
    database_url = os.getenv("AI_BRIDGE_MEMORY_DATABASE_URL", "").strip()
    rabbitmq_url = os.getenv("AI_BRIDGE_RABBITMQ_URL", "").strip()

    snapshot = build_data_plane_snapshot(database_url=database_url, rabbitmq_url=rabbitmq_url)
    print("--- POSTGRES STATUS ---")
    print(json.dumps(snapshot.as_dict(), ensure_ascii=True, default=str, indent=2))

    probe = postgres_read_write_probe(database_url)
    print("\n--- POSTGRES ROUNDTRIP PROBE ---")
    print(json.dumps(probe, ensure_ascii=True, default=str, indent=2))

    for table in ["memories", "vfs_files", "json_themes", "commands", "sessions", "users", "user_roles"]:
        rows = fetch_recent_rows(database_url, table, limit=5)
        _print_table(table, rows)

    if probe.get("ok") and snapshot.ok:
        print("\nRESULT: data is persisted and readable")
        return 0

    print("\nRESULT: database or persistence path is unhealthy")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
