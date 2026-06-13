from __future__ import annotations

import asyncio
import hashlib
import uuid
import json
import logging
import os
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import Enum
from pathlib import Path
from threading import Lock
from typing import Any, Dict, Optional

import asyncpg

from .kernel_api import KernelAPI
from .models import AgentResult, Task
from .persistent_memory import AI_BRIDGE_SCHEMA, normalize_database_url

logger = logging.getLogger("unified_vfs_memory")


class StateIntegrity(str, Enum):
    VALID = "valid"
    CORRUPTED = "corrupted"
    STALE = "stale"
    MISSING = "missing"


@dataclass(slots=True)
class VFSJournal:
    journal_path: Path | None = None

    def append(self, entry: dict[str, Any]) -> None:
        if self.journal_path is None:
            return
        self.journal_path.parent.mkdir(parents=True, exist_ok=True)
        with self.journal_path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry, default=str, sort_keys=True) + "\n")


@dataclass(slots=True)
class VFSNode:
    path: str
    content: Any
    checksum: str
    last_updated: str
    owner_agent: str
    integrity: StateIntegrity = StateIntegrity.VALID
    metadata: Dict[str, Any] = field(default_factory=dict)


class UnifiedVFSModule:
    """Unified resilient VFS with file fallback and optional PostgreSQL sync."""

    name: str = "unified_vfs"

    def __init__(self) -> None:
        self._api: KernelAPI | None = None
        self._nodes: Dict[str, VFSNode] = {}
        self._memory_lock = Lock()
        self._db_pool: asyncpg.Pool | None = None
        self._database_url: str = ""
        self._pg_enabled: bool = False
        self._loop: asyncio.AbstractEventLoop | None = None
        self.storage_root: str = "memory_store/vfs"
        self._root_path = Path(self.storage_root)
        self._artifacts_path = self._root_path / "artifacts"
        self._journal_path = self._root_path / "journal.wal"
        self._journal = VFSJournal(self._journal_path)

    async def on_load(self, api: KernelAPI | None) -> None:
        self._api = api
        try:
            self._loop = asyncio.get_running_loop()
        except RuntimeError:
            self._loop = None
        self._database_url = os.getenv("AI_BRIDGE_MEMORY_DATABASE_URL", "").strip()
        self._pg_enabled = bool(self._database_url)
        if self._pg_enabled:
            self._db_pool = await asyncpg.create_pool(dsn=normalize_database_url(self._database_url))
        if self._api:
            self._api.log("info", f"[VFS] {self.name} initialized. PostgreSQL: {self._pg_enabled}")
        self._recover_all_states()

    async def on_unload(self) -> None:
        if self._db_pool:
            await self._db_pool.close()

    def _calculate_checksum(self, content: Any) -> str:
        data = json.dumps(content, sort_keys=True, default=str).encode("utf-8")
        return hashlib.sha256(data).hexdigest()

    def _safe_path(self, path: str) -> Path:
        return self._root_path / f"{path.replace('/', '_')}.json"

    def _artifact_path(self, path: str) -> Path:
        return self._artifacts_path / f"{path.replace('/', '_')}.{uuid.uuid4().hex}.json"

    def _stabilize_content(self, content: Any) -> tuple[Any, dict[str, Any]]:
        artifacts: dict[str, Any] = {}
        if isinstance(content, dict) and isinstance(content.get("output"), dict):
            output = dict(content["output"])
            summary = output.get("summary")
            if isinstance(summary, str) and len(summary) > 1024:
                artifact_key = f"summary_{uuid.uuid4().hex}"
                artifacts[artifact_key] = summary
                artifact_file = self._artifact_path(artifact_key)
                artifact_file.parent.mkdir(parents=True, exist_ok=True)
                artifact_file.write_text(summary, encoding="utf-8")
                output["summary"] = {"$vfs_artifact": artifact_file.name, "length": len(summary)}
                content = dict(content)
                content["output"] = output
        return content, artifacts

    def _restore_content(self, content: Any) -> Any:
        if isinstance(content, dict) and isinstance(content.get("output"), dict):
            output = dict(content["output"])
            summary = output.get("summary")
            if isinstance(summary, dict) and "$vfs_artifact" in summary:
                artifact_file = self._artifacts_path / str(summary["$vfs_artifact"])
                if artifact_file.exists():
                    output["summary"] = artifact_file.read_text(encoding="utf-8")
                    content = dict(content)
                    content["output"] = output
        return content

    def _db_write(self, path: str, content: Any, checksum: str, now: datetime, agent_id: str, metadata: Dict[str, Any]) -> None:
        if not self._db_pool:
            return

        async def _write() -> None:
            async with self._db_pool.acquire() as conn:
                await conn.execute(
                    f"""
                    INSERT INTO {AI_BRIDGE_SCHEMA}.vfs_files (
                        file_path, content, checksum, last_updated, owner_agent, integrity, metadata
                    )
                    VALUES ($1, $2, $3, $4, $5, $6, $7::jsonb)
                    ON CONFLICT (file_path) DO UPDATE SET
                        content = EXCLUDED.content,
                        checksum = EXCLUDED.checksum,
                        last_updated = EXCLUDED.last_updated,
                        owner_agent = EXCLUDED.owner_agent,
                        integrity = EXCLUDED.integrity,
                        metadata = EXCLUDED.metadata,
                        updated_at = CURRENT_TIMESTAMP
                    """,
                    path,
                    json.dumps(content).encode("utf-8"),
                    checksum,
                    now,
                    agent_id,
                    StateIntegrity.VALID.value,
                    json.dumps(metadata),
                )

        if self._loop and self._loop.is_running():
            asyncio.run_coroutine_threadsafe(_write(), self._loop).result(timeout=5)
        else:
            asyncio.run(_write())

    def _db_read(self, path: str) -> dict[str, Any] | None:
        if not self._db_pool:
            return None

        async def _read() -> dict[str, Any] | None:
            async with self._db_pool.acquire() as conn:
                row = await conn.fetchrow(
                    f"""
                    SELECT content, checksum, last_updated, owner_agent, integrity, metadata
                    FROM {AI_BRIDGE_SCHEMA}.vfs_files
                    WHERE file_path = $1
                    """,
                    path,
                )
                if not row:
                    return None
                return dict(row)

        if self._loop and self._loop.is_running():
            return asyncio.run_coroutine_threadsafe(_read(), self._loop).result(timeout=5)
        return asyncio.run(_read())

    def _db_mark_corrupted(self, path: str) -> None:
        if not self._db_pool:
            return

        async def _mark() -> None:
            async with self._db_pool.acquire() as conn:
                await conn.execute(
                    f"UPDATE {AI_BRIDGE_SCHEMA}.vfs_files SET integrity = $1 WHERE file_path = $2",
                    StateIntegrity.CORRUPTED.value,
                    path,
                )

        if self._loop and self._loop.is_running():
            asyncio.run_coroutine_threadsafe(_mark(), self._loop).result(timeout=5)
        else:
            asyncio.run(_mark())

    def _recover_all_states(self) -> None:
        if not self._db_pool:
            return

        async def _recover() -> list[dict[str, Any]]:
            async with self._db_pool.acquire() as conn:
                rows = await conn.fetch(
                    f"""
                    SELECT file_path, content, checksum, last_updated, owner_agent, integrity, metadata
                    FROM {AI_BRIDGE_SCHEMA}.vfs_files
                    """
                )
                return [dict(row) for row in rows]

        try:
            rows = asyncio.run_coroutine_threadsafe(_recover(), self._loop).result(timeout=10) if self._loop and self._loop.is_running() else asyncio.run(_recover())
        except Exception as exc:
            if self._api:
                self._api.log("warning", f"[VFS] State recovery skipped during load: {exc}")
            rows = []
        with self._memory_lock:
            for row in rows:
                self._nodes[row["file_path"]] = VFSNode(
                    path=row["file_path"],
                    content=json.loads(row["content"].decode("utf-8")),
                    checksum=row["checksum"],
                    last_updated=row["last_updated"],
                    owner_agent=row["owner_agent"],
                    integrity=StateIntegrity(row["integrity"]),
                    metadata=row["metadata"],
                )

    def write_state(self, path: str, content: Any, agent_id: str, metadata: Optional[Dict[str, Any]] = None) -> bool:
        original_content = content
        checksum = self._calculate_checksum(original_content)
        content, _artifacts = self._stabilize_content(content)
        now = datetime.now(UTC)
        metadata = metadata or {}
        payload = {
            "path": path,
            "content": content,
            "checksum": checksum,
            "last_updated": now.isoformat(),
            "owner_agent": agent_id,
            "integrity": StateIntegrity.VALID.value,
            "metadata": metadata,
        }

        try:
            self._root_path.mkdir(parents=True, exist_ok=True)
            self._artifacts_path.mkdir(parents=True, exist_ok=True)
            safe_file = self._safe_path(path)
            if self._db_pool:
                self._db_write(path, content, checksum, now, agent_id, metadata)
            else:
                safe_file.write_text(json.dumps(payload, ensure_ascii=False, sort_keys=True), encoding="utf-8")
            with self._memory_lock:
                self._nodes[path] = VFSNode(path, original_content, checksum, now.isoformat(), agent_id, StateIntegrity.VALID, metadata)
            self._journal.append({"event": "write_state", **payload})
            if self._api:
                self._api.emit_event("VFS_STATE_UPDATE", {"path": path, "agent": agent_id})
            return True
        except Exception as e:
            logger.error(f"VFS write failed: {path} -> {e}")
            return False

    def read_state(self, path: str) -> Optional[VFSNode]:
        with self._memory_lock:
            node = self._nodes.get(path)
        if node:
            content = node.content
            if isinstance(content, dict) and isinstance(content.get("output"), dict):
                output = dict(content["output"])
                summary = output.get("summary")
                if isinstance(summary, dict) and "$vfs_artifact" in summary:
                    artifact_file = self._artifacts_path / str(summary["$vfs_artifact"])
                    if artifact_file.exists():
                        output["summary"] = artifact_file.read_text(encoding="utf-8")
                        content = dict(content)
                        content["output"] = output
                        node = VFSNode(node.path, content, node.checksum, node.last_updated, node.owner_agent, node.integrity, node.metadata)
            if self._calculate_checksum(node.content) == node.checksum:
                return node
            with self._memory_lock:
                self._nodes.pop(path, None)

        safe_file = self._safe_path(path)
        if safe_file.exists() and not self._db_pool:
            try:
                data = json.loads(safe_file.read_text(encoding="utf-8"))
                content = data["content"]
                if isinstance(content, dict) and isinstance(content.get("output"), dict):
                    output = dict(content["output"])
                    summary = output.get("summary")
                    if isinstance(summary, dict) and "$vfs_artifact" in summary:
                        artifact_file = self._artifacts_path / str(summary["$vfs_artifact"])
                        if artifact_file.exists():
                            output["summary"] = artifact_file.read_text(encoding="utf-8")
                            content = dict(content)
                            content["output"] = output
                node = VFSNode(
                    path=path,
                    content=content,
                    checksum=data["checksum"],
                    last_updated=data["last_updated"],
                    owner_agent=data["owner_agent"],
                    integrity=StateIntegrity(data["integrity"]),
                    metadata=data.get("metadata", {}),
                )
                if self._calculate_checksum(node.content) != node.checksum:
                    safe_file.unlink(missing_ok=True)
                    return None
                with self._memory_lock:
                    self._nodes[path] = node
                return node
            except Exception as e:
                logger.error(f"VFS file read failed: {path} -> {e}")
                return None

        if self._db_pool:
            try:
                row = self._db_read(path)
                if not row:
                    return None
                content = json.loads(row["content"].decode("utf-8"))
                restored_content = self._restore_content(content)
                node = VFSNode(
                    path=path,
                    content=restored_content,
                    checksum=row["checksum"],
                    last_updated=row["last_updated"],
                    owner_agent=row["owner_agent"],
                    integrity=StateIntegrity(row["integrity"]),
                    metadata=row["metadata"],
                )
                if self._calculate_checksum(node.content) != node.checksum:
                    logger.error(f"[VFS] Integrity violation at {path}! Checksum mismatch.")
                    self._db_mark_corrupted(path)
                    return None
                with self._memory_lock:
                    self._nodes[path] = node
                return node
            except Exception as e:
                logger.error(f"VFS DB read failed: {path} -> {e}")
                return None

        return None

    def before_task(self, task: Task, context: Dict[str, Any]) -> None:
        resume_path = f"active_tasks/{task.task_id}/checkpoint"
        node = self.read_state(resume_path)
        if node and node.integrity == StateIntegrity.VALID:
            context["recovered_state"] = node.content
            if self._api:
                self._api.log("info", f"[VFS] Recovered state for task {task.task_id} from {node.owner_agent}")

    def after_task(self, task: Task, result: AgentResult, context: Dict[str, Any]) -> None:
        path = f"active_tasks/{task.task_id}/checkpoint"
        output = result.output.as_dict() if hasattr(result.output, "as_dict") else result.output
        state = {
            "status": result.status.value,
            "output": output,
            "intermediate_artifacts": context.get("intermediate_artifacts", []),
            "last_step": context.get("last_step", "completed"),
        }
        self.write_state(path, state, result.agent_id)

    def finalize(self) -> Dict[str, Any]:
        return {
            "node_count": len(self._nodes),
            "storage": f"postgresql:{AI_BRIDGE_SCHEMA}.vfs_files",
            "integrity": "healthy",
        }
