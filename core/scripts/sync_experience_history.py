from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.core.persistent_memory import PersistentMemoryManager


@dataclass(slots=True)
class _Settings:
    database_url: str = ""
    etcd_url: str = ""
    etcd_prefix: str = "/core"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Sync trained history and KPI memories from PostgreSQL into file-backed memory_store")
    parser.add_argument("--source-dsn", required=True, help="PostgreSQL DSN for the source memory store")
    parser.add_argument("--target-dir", default="memory_store/synced_pg_history", help="Target file-backed memory directory")
    parser.add_argument("--limit", type=int, default=1000, help="Max rows per source collection")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    source = PersistentMemoryManager(_Settings(database_url=args.source_dsn))
    target_dir = Path(args.target_dir)
    target_dir.mkdir(parents=True, exist_ok=True)

    import os
    previous = os.environ.get("AI_BRIDGE_MEMORY_STORE_DIR")
    os.environ["AI_BRIDGE_MEMORY_STORE_DIR"] = str(target_dir)
    try:
        target = PersistentMemoryManager(_Settings(database_url=""))
    finally:
        if previous is None:
            os.environ.pop("AI_BRIDGE_MEMORY_STORE_DIR", None)
        else:
            os.environ["AI_BRIDGE_MEMORY_STORE_DIR"] = previous

    synced = {"trained_memories": 0, "kpi_memories": 0}

    for row in source.list_trained_memories(limit=args.limit):
        content = getattr(row, "content", {})
        metadata = dict(getattr(row, "metadata", {}) or {})
        source_session_id = str(content.get("source_session_id") or row.session_id or "synced-session") if isinstance(content, dict) else str(row.session_id)
        target.store_trained_memory(
            session_id=source_session_id,
            agent_id=row.agent_id,
            memory_domain=row.memory_domain,
            content=content,
            source_memory_ids=list(getattr(row, "source_memory_ids", []) or []),
            metadata=metadata,
            quality_score=float(getattr(row, "quality_score", 0.0) or 0.0),
        )
        synced["trained_memories"] += 1

    for row in source.list_memories(limit=args.limit, memory_type_prefix="kpi_task:"):
        metadata = dict(getattr(row, "metadata", {}) or {})
        content = getattr(row, "content", {})
        session_id = metadata.get("source_session_id") or row.session_id or "synced-kpi-session"
        target.store_memory(
            session_id=str(session_id),
            agent_id=row.agent_id,
            memory_type=row.memory_type,
            content=content,
            metadata=metadata,
            importance_score=float(getattr(row, "importance_score", 0.5) or 0.5),
        )
        synced["kpi_memories"] += 1

    print(json.dumps({"status": "ok", "target_dir": str(target_dir), **synced}, ensure_ascii=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
