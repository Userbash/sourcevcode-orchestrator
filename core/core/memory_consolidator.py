from __future__ import annotations

import logging

from .hybrid_memory import HybridMemory

logger = logging.getLogger(__name__)


class MemoryConsolidator:
    def __init__(self, hybrid_memory: HybridMemory) -> None:
        self.hybrid_memory = hybrid_memory

    def consolidate(self, *, session_id: str, agent_id: str) -> str | None:
        try:
            return self.hybrid_memory.persistent.consolidate_episodic(
                session_id=session_id,
                agent_id=agent_id,
                chunk_size=5,
            )
        except Exception:
            logger.exception("Memory consolidation failed")
            return None

    def consolidate_successful_task(
        self,
        *,
        session_id: str,
        agent_id: str,
        task_type: str,
        summary: str,
        source_memory_ids: list[int] | None = None,
        quality_score: float = 0.0,
        metadata: dict[str, object] | None = None,
    ) -> str | None:
        try:
            persistent = self.hybrid_memory.persistent
            if hasattr(persistent, "consolidate_successful_task"):
                return persistent.consolidate_successful_task(
                    session_id=session_id,
                    agent_id=agent_id,
                    task_type=task_type,
                    summary=summary,
                    source_memory_ids=source_memory_ids or [],
                    quality_score=quality_score,
                    metadata=metadata or {},
                )
            return None
        except Exception:
            logger.exception("Training consolidation failed")
            return None
