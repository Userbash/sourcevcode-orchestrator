from __future__ import annotations

import json
import logging
import hashlib
import asyncio
import os
import uuid
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Optional
from threading import Lock

import asyncpg

from .kernel_api import KernelAPI
from .models import AgentResult, Task, TaskStatus
from .persistent_memory import AI_BRIDGE_SCHEMA, normalize_database_url

logger = logging.getLogger("unified_vfs_memory")

class StateIntegrity(str, Enum):
    VALID = "valid"
    CORRUPTED = "corrupted"
    STALE = "stale"
    MISSING = "missing"

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
    """
    Unified Resilient Memory VFS (Virtual File System).
    Provides a shared, validated JSON space for agent synchronization,
    backed by PostgreSQL using asynchronous I/O.
    """
    name: str = "unified_vfs"
    
    def __init__(self):
        self._api: KernelAPI | None = None
        self._nodes: Dict[str, VFSNode] = {}
        self._memory_lock = Lock()
        self._db_pool: asyncpg.Pool | None = None
        self._database_url: str = ""
        self._pg_enabled: bool = False
        self._loop: asyncio.AbstractEventLoop | None = None

    async def on_load(self, api: KernelAPI) -> None:
        self._api = api
        self._loop = asyncio.get_running_loop()
        self._database_url = os.getenv("AI_BRIDGE_MEMORY_DATABASE_URL", "").strip()
        self._pg_enabled = bool(self._database_url)
        if self._pg_enabled:
            self._db_pool = await asyncpg.create_pool(dsn=normalize_database_url(self._database_url))
        
        if self._api:
            self._api.log("info", f"[VFS] {self.name} initialized. PostgreSQL: {self._pg_enabled}")
        await self._recover_all_states()

    async def on_unload(self) -> None:
        if self._db_pool:
            await self._db_pool.close()

    def _calculate_checksum(self, content: Any) -> str:
        data = json.dumps(content, sort_keys=True, default=str).encode("utf-8")
        return hashlib.sha256(data).hexdigest()

    async def write_state(self, path: str, content: Any, agent_id: str, metadata: Optional[Dict] = None) -> bool:
        """Atomic write to VFS DB asynchronously."""
        if not self._db_pool:
            return False
            
        checksum = self._calculate_checksum(content)
        now = datetime.now(UTC)
        
        try:
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
                    json.dumps(metadata or {})
                )
            
            with self._memory_lock:
                self._nodes[path] = VFSNode(
                    path=path,
                    content=content,
                    checksum=checksum,
                    last_updated=now,
                    owner_agent=agent_id,
                    metadata=metadata or {}
                )
                
            if self._api:
                self._api.emit_event("VFS_STATE_UPDATE", {"path": path, "agent": agent_id})
            return True
        except Exception as e:
            logger.error(f"VFS DB Async Write Failed: {path} -> {e}")
            return False

    async def read_state(self, path: str) -> Optional[VFSNode]:
        """Read state from VFS DB asynchronously with integrity check."""
        with self._memory_lock:
            node = self._nodes.get(path)
            
        if node:
            # Re-verify integrity for cached node
            if self._calculate_checksum(node.content) == node.checksum:
                return node
            else:
                with self._memory_lock:
                    del self._nodes[path]

        if not self._db_pool:
            return None
            
        try:
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
                        
                content = json.loads(row["content"].decode("utf-8"))
                node = VFSNode(
                    path=path,
                    content=content,
                    checksum=row["checksum"],
                    last_updated=row["last_updated"],
                    owner_agent=row["owner_agent"],
                    integrity=StateIntegrity(row["integrity"]),
                    metadata=row["metadata"]
                )
            
            # Verify integrity from DB
            if self._calculate_checksum(node.content) != node.checksum:
                logger.error(f"[VFS] Integrity violation at {path}! Checksum mismatch.")
                # Mark as corrupted in DB
                async with self._db_pool.acquire() as conn:
                    await conn.execute(f"UPDATE {AI_BRIDGE_SCHEMA}.vfs_files SET integrity = $1 WHERE file_path = $2", StateIntegrity.CORRUPTED.value, path)
                return None

            with self._memory_lock:
                self._nodes[path] = node
            return node
        except Exception as e:
            logger.error(f"VFS DB Async Read Failed: {path} -> {e}")
            return None

    async def _recover_all_states(self) -> None:
        """Cold boot recovery from DB asynchronously."""
        if not self._db_pool:
            return

        try:
            async with self._db_pool.acquire() as conn:
                rows = await conn.fetch(
                    f"""
                    SELECT file_path, content, checksum, last_updated, owner_agent, integrity, metadata
                    FROM {AI_BRIDGE_SCHEMA}.vfs_files
                    """
                )
                with self._memory_lock:
                    for row in rows:
                        self._nodes[row["file_path"]] = VFSNode(
                            path=row["file_path"],
                            content=json.loads(row["content"].decode("utf-8")),
                            checksum=row["checksum"],
                            last_updated=row["last_updated"],
                            owner_agent=row["owner_agent"],
                            integrity=StateIntegrity(row["integrity"]),
                            metadata=row["metadata"]
                        )
        except Exception as e:
            logger.error(f"Failed to recover state from DB: {e}")

    def before_task(self, task: Task, context: Dict[str, Any]) -> None:
        resume_path = f"active_tasks/{task.task_id}/checkpoint"
        if self._loop is None:
             return

        # Run async read_state synchronously in the main loop from current thread
        try:
            future = asyncio.run_coroutine_threadsafe(self.read_state(resume_path), self._loop)
            node = future.result(timeout=5)
            if node and node.integrity == StateIntegrity.VALID:
                context["recovered_state"] = node.content
                if self._api:
                    self._api.log("info", f"[VFS] Recovered state for task {task.task_id} from {node.owner_agent}")
        except Exception as e:
            logger.error(f"[VFS] Sync read failed in before_task: {e}")

    def after_task(self, task: Task, result: AgentResult, context: Dict[str, Any]) -> None:
        path = f"active_tasks/{task.task_id}/checkpoint"
        if self._loop is None:
             return

        output = result.output.as_dict() if hasattr(result.output, "as_dict") else result.output
        state = {
            "status": result.status.value,
            "output": output,
            "intermediate_artifacts": context.get("intermediate_artifacts", []),
            "last_step": context.get("last_step", "completed")
        }
        # Run async write_state synchronously in the main loop from current thread
        try:
            future = asyncio.run_coroutine_threadsafe(self.write_state(path, state, result.agent_id), self._loop)
            future.result(timeout=5)
        except Exception as e:
            logger.error(f"[VFS] Sync write failed in after_task: {e}")

    def finalize(self) -> Dict[str, Any]:
        return {
            "node_count": len(self._nodes),
            "storage": f"postgresql:{AI_BRIDGE_SCHEMA}.vfs_files",
            "integrity": "healthy"
        }
