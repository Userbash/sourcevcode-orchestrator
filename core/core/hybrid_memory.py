from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from .memory_backend import BackendEntry, InMemoryBackend, MemoryBackend
from .memory_settings import MemorySettings
from .persistent_memory import PersistentMemoryManager

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class MemoryStub:
    memory_id: int | None
    summary: str
    persisted: bool


@dataclass(slots=True)
class HotEntry:
    key: str
    value: Any
    scope: str
    identifier: str
    created_at: datetime
    last_accessed: datetime
    expires_at: datetime | None
    importance_score: float = 0.5
    access_count: int = 0
    memory_type: str = "episodic"
    tags: list[str] = field(default_factory=list)
    indexed_terms: set[str] = field(default_factory=set)
    stub: MemoryStub | None = None
    vfs_warm: bool = False


@dataclass(slots=True)
class RetrievalHit:
    key: str
    value: Any
    score: float
    semantic_similarity: float
    importance_score: float
    time_decay: float


class HybridMemory:
    def __init__(self, settings: MemorySettings | None = None, backend: MemoryBackend | None = None, persistent: PersistentMemoryManager | None = None) -> None:
        self.settings = settings or MemorySettings.from_env()
        self.backend = backend or InMemoryBackend()
        self.persistent = persistent or PersistentMemoryManager(self.settings)
        self._hot: dict[str, HotEntry] = {}
        self._term_index: dict[str, set[str]] = {}
        self._session_index: dict[str, set[str]] = {}
        self._project_index: dict[str, set[str]] = {}
        self._maintenance_task: asyncio.Task[None] | None = None
        self._trained_memory_brief_cache: dict[tuple[str, str, str, int, int], tuple[datetime, str]] = {}
        self._trained_memory_quality_threshold = float(getattr(self.settings, "trained_memory_quality_threshold", 0.75) or 0.75)
        self._trained_memory_quality_thresholds_by_task = dict(getattr(self.settings, "trained_memory_quality_thresholds_by_task", {}) or {})
        self._trained_memory_brief_ttl_sec = max(60, int(getattr(self.settings, "trained_memory_brief_ttl_sec", 600) or 600))
        self._trained_memory_degrade: dict[tuple[str, str], datetime] = {}
        self._trained_memory_degrade_ttl_sec = max(120, int(getattr(self.settings, "trained_memory_degrade_ttl_sec", 900) or 900))
        self._trained_memory_degrade_releases = 0
        self._trained_memory_outcome_stats: dict[tuple[str, str], dict[str, int]] = {}

    @staticmethod
    def make_key(scope: str, identifier: str, key: str) -> str:
        return f"{scope}:{identifier}:{key}"

    @staticmethod
    def session_state_key(session_id: str) -> str:
        return f"session:{session_id}:state"

    @staticmethod
    def session_env_key(session_id: str) -> str:
        return f"session:{session_id}:context:env"

    @staticmethod
    def session_agent_thoughts_key(session_id: str, agent_id: str) -> str:
        return f"session:{session_id}:agent:{agent_id}:thoughts"

    @staticmethod
    def session_agent_errors_key(session_id: str, agent_id: str) -> str:
        return f"session:{session_id}:agent:{agent_id}:errors"

    @staticmethod
    def domain_patterns_key(project_name: str) -> str:
        return f"memory:domain:{project_name}:patterns"

    @staticmethod
    def capability_practices_key(capability: str) -> str:
        return f"memory:capability:{capability}:best_practices"

    @staticmethod
    def task_artifacts_diff_key(task_id: str) -> str:
        return f"task:{task_id}:artifacts:diff"

    @staticmethod
    def task_metrics_perf_key(task_id: str) -> str:
        return f"task:{task_id}:metrics:perf"

    def get(self, scope: str, identifier: str, key: str) -> Any | None:
        skey = self.make_key(scope, identifier, key)
        entry = self._hot.get(skey)
        if entry:
            if entry.expires_at and datetime.now(UTC) >= entry.expires_at:
                self._drop_key(skey)
                return None
            entry.access_count += 1
            entry.last_accessed = datetime.now(UTC)
            backend_entry = self.backend.get(skey)
            return backend_entry.value if backend_entry else entry.value
        return self._restore_from_persistent(scope, identifier, key)

    def set(self, scope: str, identifier: str, key: str, value: Any, *, expires_at: datetime | None = None, importance_score: float = 0.5, memory_type: str = "episodic", tags: list[str] | None = None) -> None:
        now = datetime.now(UTC)
        skey = self.make_key(scope, identifier, key)
        if skey in self._hot:
            self._remove_indexes(skey, self._hot[skey])

        terms = self._entry_terms(skey, value)
        self._hot[skey] = HotEntry(
            key=key,
            value=value,
            scope=scope,
            identifier=identifier,
            created_at=now,
            last_accessed=now,
            expires_at=expires_at,
            importance_score=max(0.0, min(1.0, importance_score)),
            memory_type=memory_type,
            tags=tags or [],
            indexed_terms=terms,
        )
        self.backend.set(skey, BackendEntry(value=value, created_at=now, expires_at=expires_at, last_accessed_at=now))
        self._add_indexes(skey, self._hot[skey])
        if len(self._hot) > self.settings.hot_cache_max_entries:
            self.run_maintenance_once()

    def get_by_full_key(self, full_key: str) -> Any | None:
        entry = self._hot.get(full_key)
        if entry:
            return entry.value
        backend_entry = self.backend.get(full_key)
        return backend_entry.value if backend_entry else None

    def set_by_full_key(self, full_key: str, value: Any, *, expires_at: datetime | None = None, importance_score: float = 0.5, memory_type: str = "episodic", tags: list[str] | None = None) -> None:
        now = datetime.now(UTC)
        chunks = full_key.split(":")
        scope = chunks[0] if chunks else "global"
        identifier = chunks[1] if len(chunks) > 1 else "default"
        key = ":".join(chunks[2:]) if len(chunks) > 2 else full_key
        if full_key in self._hot:
            self._remove_indexes(full_key, self._hot[full_key])

        terms = self._entry_terms(full_key, value)
        self._hot[full_key] = HotEntry(
            key=key,
            value=value,
            scope=scope,
            identifier=identifier,
            created_at=now,
            last_accessed=now,
            expires_at=expires_at,
            importance_score=max(0.0, min(1.0, importance_score)),
            memory_type=memory_type,
            tags=tags or [],
            indexed_terms=terms,
        )
        self.backend.set(full_key, BackendEntry(value=value, created_at=now, expires_at=expires_at, last_accessed_at=now))
        self._add_indexes(full_key, self._hot[full_key])

    def append_agent_thought(self, *, session_id: str, agent_id: str, thought: str) -> None:
        key = self.session_agent_thoughts_key(session_id, agent_id)
        thoughts = self.get_by_full_key(key) or []
        if not isinstance(thoughts, list):
            thoughts = [str(thoughts)]
        thoughts.append(thought)
        self.set_by_full_key(key, thoughts, importance_score=0.4, memory_type="thought")

    def append_agent_error(self, *, session_id: str, agent_id: str, error: str) -> None:
        key = self.session_agent_errors_key(session_id, agent_id)
        errors = self.get_by_full_key(key) or []
        if not isinstance(errors, list):
            errors = [str(errors)]
        errors.append(error)
        self.set_by_full_key(key, errors, importance_score=0.8, memory_type="error")

    def clear_session_thoughts(self, *, session_id: str) -> int:
        prefix = f"session:{session_id}:agent:"
        removed = 0
        for key in list(self._hot.keys()):
            if key.startswith(prefix) and (key.endswith(":thoughts") or key.endswith(":errors")):
                self._drop_key(key)
                removed += 1
        return removed

    def diagnostic_snapshot(self) -> dict[str, Any]:
        hot_keys = list(self._hot.keys())
        backend_keys = []
        if hasattr(self.backend, "keys"):
            try:
                backend_keys = list(self.backend.keys())
            except Exception:
                backend_keys = []
        persistent_enabled = bool(getattr(self.persistent, "_pg_enabled", False))
        persistent_url = getattr(self.persistent, "database_url", "")
        return {
            "hot_count": len(hot_keys),
            "backend_count": len(backend_keys),
            "hot_keys": hot_keys[:25],
            "backend_keys": backend_keys[:25],
            "persistent_enabled": persistent_enabled,
            "persistent_url": persistent_url,
            "session_index_count": len(self._session_index),
            "project_index_count": len(self._project_index),
            "term_index_count": len(self._term_index),
            "trained_memory_degrade_count": len(self._trained_memory_degrade),
            "trained_memory_degrade_releases": self._trained_memory_degrade_releases,
        }

    def fast_retrieve(
        self,
        *,
        query_text: str,
        session_id: str | None = None,
        project_name: str | None = None,
        top_k: int = 3,
        api: Any | None = None,
    ) -> list[RetrievalHit]:
        now = datetime.now(UTC)
        hits: list[RetrievalHit] = []

        # 1. Semantic Search (Vector) if LLM is available
        query_vector: list[float] = []
        if api:
            local_llm = api.get_module("local_llm")
            if local_llm and getattr(local_llm, "ready", False):
                try:
                    # In a real system, we'd use a dedicated embedding endpoint.
                    # Here we use the LLM to get a representation or just tokens.
                    # For prototype, we'll keep using the token-based fallback but with higher weights.
                    pass
                except Exception:
                    pass

        norm_query_terms = set(self._tokenize(query_text))
        candidate_keys = self._candidate_keys(norm_query_terms, session_id=session_id, project_name=project_name)
        
        for full_key in candidate_keys:
            entry = self._hot.get(full_key)
            if not entry:
                continue
            
            semantic_similarity = self._semantic_similarity(norm_query_terms, list(entry.indexed_terms))
            age_sec = max(1.0, (now - entry.last_accessed).total_seconds())
            
            # Time Decay: Newer is better (Half-life: 1 hour)
            time_decay = 1.0 / (1.0 + age_sec / 3600.0)
            
            # Weighted Scoring: Semantic (50%) + Importance (30%) + Recency (20%)
            score = 0.5 * semantic_similarity + 0.3 * entry.importance_score + 0.2 * time_decay
            
            hits.append(
                RetrievalHit(
                    key=full_key,
                    value=entry.value,
                    score=score,
                    semantic_similarity=semantic_similarity,
                    importance_score=entry.importance_score,
                    time_decay=time_decay,
                )
            )

        hits.sort(key=lambda x: x.score, reverse=True)
        return hits[: max(1, top_k)]

    def build_context_brief(self, *, hits: list[RetrievalHit], token_limit: int = 1500) -> str:
        """Compresses retrieved memory into a surgically precise context brief."""
        budget_chars = max(200, token_limit * 4)
        lines: list[str] = [f"--- RELEVANT MEMORY (Top {len(hits)}) ---"]
        used = len(lines[0])
        
        for hit in hits:
            # Format: [Score: 0.85] [Scope: session] key: content...
            score_tag = f"[Relevance: {hit.score:.2f}]"
            content = str(hit.value)
            if len(content) > 500:
                content = content[:497] + "..."
            
            line = f"{score_tag} {hit.key}: {content}"
            if used + len(line) + 1 > budget_chars:
                break
            lines.append(line)
            used += len(line) + 1
            
        return "\n".join(lines)

    def delete(self, scope: str, identifier: str, key: str) -> None:
        skey = self.make_key(scope, identifier, key)
        self._drop_key(skey)

    def list_keys(self) -> list[str]:
        return list(self._hot.keys())

    def invalidate(self, prefix: str | None = None) -> int:
        removed = 0
        for skey in list(self._hot.keys()):
            if prefix and not skey.startswith(prefix):
                continue
            self._drop_key(skey)
            removed += 1
        return removed

    def clear(self) -> None:
        self._hot.clear()
        self._term_index.clear()
        self._session_index.clear()
        self._project_index.clear()
        self.backend.clear()

    def soft_flush(self, api: Any | None = None) -> int:
        """Persist all hot entries and buffered records with AI-driven compaction."""
        flushed = 0
        local_llm = api.get_module("local_llm") if api else None
        
        # Batch events for compaction if many
        if len(self._hot) > 10 and local_llm and getattr(local_llm, "ready", False):
            try:
                # Group by session for compaction
                raw_logs = [{"key": entry.key, "value": str(entry.value)} for entry in self._hot.values()]
                summary = local_llm.compact_memory(raw_logs)
                # Store summary as a special 'Anchor' memory
                self.set("session", "system", "archive_summary", summary, importance_score=0.9, memory_type="anchor")
            except Exception:
                pass

        for _, entry in list(self._hot.items()):
            # Use AI to generate indexing keywords if missing
            if not entry.tags and local_llm and getattr(local_llm, "ready", False):
                entry.tags = local_llm.generate_embedding_keywords(str(entry.value))

            memory_id = self.persistent.store_memory(
                session_id=entry.identifier,
                agent_id=self._persistence_agent_id(entry.scope, entry.identifier),
                memory_type=entry.memory_type,
                content=self.persistent.serialize_payload(entry.value),
                importance_score=entry.importance_score,
                metadata={"key": entry.key, "scope": entry.scope, "tags": entry.tags, "vfs_warm": entry.vfs_warm},
                expires_at=entry.expires_at,
            )
            if memory_id:
                entry.stub = MemoryStub(memory_id=memory_id, summary=str(entry.value)[:200], persisted=True)
                flushed += 1

        if hasattr(self.persistent, "flush_all"):
            flushed += self.persistent.flush_all()

        logger.info(f"[MEMORY] Soft flush complete: {flushed} total records persisted.")
        return flushed

    def run_maintenance_once(self) -> int:
        if not self._hot:
            return 0
        now = datetime.now(UTC)
        ranked: list[tuple[float, str, HotEntry]] = []
        for skey, entry in self._hot.items():
            age_sec = max(1.0, (now - entry.created_at).total_seconds())
            idle_sec = max(1.0, (now - entry.last_accessed).total_seconds())
            recency = 1.0 / idle_sec
            access_freq = entry.access_count / age_sec
            score = 0.4 * recency + 0.3 * access_freq + 0.3 * entry.importance_score
            ranked.append((score, skey, entry))
        ranked.sort(key=lambda item: item[0])
        limit = max(1, len(ranked) // 5)
        evicted = 0
        for _, skey, entry in ranked[:limit]:
            memory_id = self._persist_entry(entry)
            entry.stub = MemoryStub(memory_id=memory_id, summary=str(entry.value)[:200], persisted=memory_id is not None)
            self._drop_key(skey)
            evicted += 1
        return evicted

    def start_background_tasks(self) -> None:
        if self._maintenance_task and not self._maintenance_task.done():
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        self._maintenance_task = loop.create_task(self._maintenance_loop())

    async def _maintenance_loop(self) -> None:
        while True:
            await asyncio.sleep(max(10, self.settings.eviction_interval_sec))
            self.run_maintenance_once()

    def remember_command(self, *, session_id: str, agent_id: str, command: str, result: dict[str, Any], success: bool, tokens_used: int | None = None) -> None:
        self.persistent.store_command(
            session_id=session_id,
            agent_id=agent_id,
            command=command,
            result=result,
            success=success,
            tokens_used=tokens_used,
        )

    def load_command_window(self, *, session_id: str, agent_id: str, limit: int | None = None) -> list[dict[str, Any]]:
        return self.persistent.list_recent_commands(
            session_id=session_id,
            agent_id=agent_id,
            limit=limit or self.settings.command_window_size,
        )

    def _trained_cache_key(self, *, session_id: str, agent_id: str, memory_domain: str, top_k: int, token_limit: int, quality_threshold: float) -> tuple[str, str, str, int, int, float]:
        return (session_id, agent_id, memory_domain, int(top_k), int(token_limit), round(float(quality_threshold), 3))

    def _trained_cache_get(self, *, session_id: str, agent_id: str, memory_domain: str, top_k: int, token_limit: int, quality_threshold: float) -> str | None:
        key = self._trained_cache_key(session_id=session_id, agent_id=agent_id, memory_domain=memory_domain, top_k=top_k, token_limit=token_limit, quality_threshold=quality_threshold)
        cached = self._trained_memory_brief_cache.get(key)
        if not cached:
            return None
        expires_at, brief = cached
        if datetime.now(UTC) >= expires_at:
            self._trained_memory_brief_cache.pop(key, None)
            return None
        return brief

    def _trained_cache_set(self, *, session_id: str, agent_id: str, memory_domain: str, top_k: int, token_limit: int, quality_threshold: float, brief: str) -> None:
        key = self._trained_cache_key(session_id=session_id, agent_id=agent_id, memory_domain=memory_domain, top_k=top_k, token_limit=token_limit, quality_threshold=quality_threshold)
        self._trained_memory_brief_cache[key] = (datetime.now(UTC) + timedelta(seconds=self._trained_memory_brief_ttl_sec), brief)

    def _trained_memory_rank(self, record: Any, *, position: int) -> float:
        score = float(getattr(record, "quality_score", 0.0) or 0.0)
        created_at = getattr(record, "created_at", "")
        age_bonus = 0.0
        if created_at:
            try:
                age_dt = datetime.fromisoformat(str(created_at).replace("Z", "+00:00"))
                age_hours = max(0.0, (datetime.now(UTC) - age_dt).total_seconds() / 3600.0)
                age_bonus = 1.0 / (1.0 + age_hours)
            except Exception:
                age_bonus = 0.0
        recency_bonus = 1.0 / (1.0 + position)
        return score * 0.7 + age_bonus * 0.2 + recency_bonus * 0.1

    def _trained_quality_threshold_for_domain(self, memory_domain: str) -> float:
        key = memory_domain.split(":", 1)[-1].lower()
        return float(self._trained_memory_quality_thresholds_by_task.get(key, self._trained_memory_quality_threshold) or self._trained_memory_quality_threshold)


    def _degrade_key(self, session_id: str, task_type: str) -> tuple[str, str]:
        return (session_id, task_type)

    def record_trained_memory_rejection(self, *, session_id: str, task_type: str, threshold: float, reason: str) -> None:
        key = self._degrade_key(session_id, task_type)
        self._trained_memory_degrade[key] = datetime.now(UTC) + timedelta(seconds=self._trained_memory_degrade_ttl_sec)
        logger.info("[MEMORY] trained memory rejected session=%s task_type=%s threshold=%.2f reason=%s", session_id, task_type, threshold, reason)

    def _trained_outcome_key(self, session_id: str, task_type: str) -> tuple[str, str]:
        return (session_id, task_type)

    def record_trained_memory_outcome(self, *, session_id: str, task_type: str, accepted: bool, threshold: float, reason: str) -> None:
        key = self._trained_outcome_key(session_id, task_type)
        stats = self._trained_memory_outcome_stats.setdefault(key, {"accepted": 0, "rejected": 0})
        if accepted:
            stats["accepted"] += 1
        else:
            stats["rejected"] += 1
        total = stats["accepted"] + stats["rejected"]
        rejection_rate = stats["rejected"] / total if total else 0.0
        if not accepted and total >= 3 and rejection_rate >= 0.67:
            self.record_trained_memory_rejection(session_id=session_id, task_type=task_type, threshold=threshold, reason=f"{reason};high_rejection_rate={rejection_rate:.2f}")

    def trained_memory_rejection_rate(self, *, session_id: str, task_type: str) -> float:
        stats = self._trained_memory_outcome_stats.get(self._trained_outcome_key(session_id, task_type), {"accepted": 0, "rejected": 0})
        total = stats["accepted"] + stats["rejected"]
        return stats["rejected"] / total if total else 0.0

    def _trained_memory_degraded(self, *, session_id: str, task_type: str) -> bool:
        key = self._degrade_key(session_id, task_type)
        expires = self._trained_memory_degrade.get(key)
        if not expires:
            return False
        if datetime.now(UTC) >= expires:
            self._trained_memory_degrade.pop(key, None)
            self._trained_memory_degrade_releases += 1
            return False
        return True

    def release_expired_trained_memory_degrade(self) -> int:
        now = datetime.now(UTC)
        released = 0
        for key, expires in list(self._trained_memory_degrade.items()):
            if now >= expires:
                self._trained_memory_degrade.pop(key, None)
                released += 1
        if released:
            self._trained_memory_degrade_releases += released
        return released

    def _rank_trained_memories(self, records: list[Any], *, top_k: int, quality_threshold: float) -> list[Any]:
        filtered = [record for record in records if float(getattr(record, "quality_score", 0.0) or 0.0) >= quality_threshold]
        if not filtered:
            return []
        ordered = sorted(
            enumerate(filtered),
            key=lambda pair: self._trained_memory_rank(pair[1], position=pair[0]),
            reverse=True,
        )
        return [record for _, record in ordered[:max(1, int(top_k))]]

    def retrieve_trained_memory_brief(
        self,
        *,
        session_id: str,
        agent_id: str,
        memory_domain: str,
        top_k: int = 3,
        token_limit: int = 900,
        task_type: str | None = None,
        allow_trained_memory: bool = True,
    ) -> str:
        normalized_task_type = str(task_type or memory_domain.split(":", 1)[-1]).lower()
        if not allow_trained_memory or self._trained_memory_degraded(session_id=session_id, task_type=normalized_task_type):
            return ""
        quality_threshold = self._trained_quality_threshold_for_domain(memory_domain)
        cached = self._trained_cache_get(
            session_id=session_id,
            agent_id=agent_id,
            memory_domain=memory_domain,
            top_k=top_k,
            token_limit=token_limit,
            quality_threshold=quality_threshold,
        )
        if cached is not None:
            self.record_trained_memory_outcome(session_id=session_id, task_type=normalized_task_type, accepted=True, threshold=quality_threshold, reason="cache_hit")
            return cached

        records: list[Any] = []
        if hasattr(self.persistent, "retrieve_trained_memories"):
            try:
                records = self.persistent.retrieve_trained_memories(
                    session_id=session_id,
                    agent_id=agent_id,
                    memory_domain=memory_domain,
                    top_k=max(1, int(top_k)) * 3,
                )
            except Exception:
                records = []
        if not records:
            return ""
        records = self._rank_trained_memories(records, top_k=top_k, quality_threshold=quality_threshold)
        if not records:
            self.record_trained_memory_outcome(session_id=session_id, task_type=normalized_task_type, accepted=False, threshold=quality_threshold, reason="quality_threshold")
            return ""

        lines = [f"--- TRAINED MEMORY BRIEF ({memory_domain}, Top {len(records)}) ---"]
        used = len(lines[0])
        budget_chars = max(200, token_limit * 4)
        for record in records:
            if isinstance(record, dict):
                content = record.get("content")
                source_ids = record.get("source_memory_ids") or []
                score = float(record.get("quality_score", 0.0) or 0.0)
                domain = str(record.get("memory_domain", memory_domain))
                label = f"[Quality: {score:.2f}] [Domain: {domain}] [Sources: {source_ids}]"
            else:
                content = getattr(record, "content", None)
                source_ids = getattr(record, "source_memory_ids", [])
                score = float(getattr(record, "quality_score", 0.0) or 0.0)
                domain = str(getattr(record, "memory_domain", memory_domain))
                label = f"[Quality: {score:.2f}] [Domain: {domain}] [Sources: {source_ids}]"
            payload = str(content)
            if len(payload) > 500:
                payload = payload[:497] + "..."
            line = f"{label} {payload}"
            if used + len(line) + 1 > budget_chars:
                break
            lines.append(line)
            used += len(line) + 1
        brief = "\n".join(lines)
        self._trained_cache_set(
            session_id=session_id,
            agent_id=agent_id,
            memory_domain=memory_domain,
            top_k=top_k,
            token_limit=token_limit,
            quality_threshold=quality_threshold,
            brief=brief,
        )
        return brief

    def get_trained_memory_context(
        self,
        *,
        session_id: str,
        agent_id: str,
        memory_domain: str,
        top_k: int = 3,
    ) -> dict[str, Any]:
        brief = self.retrieve_trained_memory_brief(
            session_id=session_id,
            agent_id=agent_id,
            memory_domain=memory_domain,
            top_k=top_k,
        )
        return {
            "brief": brief,
            "memory_domain": memory_domain,
            "session_id": session_id,
            "agent_id": agent_id,
            "has_trained_memory": bool(brief),
        }

    def use_trained_memory(
        self,
        *,
        session_id: str,
        agent_id: str,
        memory_domain: str,
        top_k: int = 3,
    ) -> str:
        return self.retrieve_trained_memory_brief(
            session_id=session_id,
            agent_id=agent_id,
            memory_domain=memory_domain,
            top_k=top_k,
        )

    def get_command_history(self, *, session_id: str, limit: int | None = None) -> list[dict[str, Any]]:
        return self.persistent.list_recent_commands_by_session(
            session_id=session_id,
            limit=limit or self.settings.command_window_size,
        )

    def _persistence_agent_id(self, scope: str, identifier: str) -> str:
        if scope == "agent":
            return identifier
        return f"{scope}-memory"

    def _persist_entry(self, entry: HotEntry) -> int | None:
        persistence_agent_id = self._persistence_agent_id(entry.scope, entry.identifier)
        return self.persistent.store_memory(
            session_id=entry.identifier,
            agent_id=persistence_agent_id,
            memory_type=entry.memory_type,
            content=self.persistent.serialize_payload(entry.value),
            importance_score=entry.importance_score,
            metadata={"key": entry.key, "scope": entry.scope, "tags": entry.tags},
            expires_at=entry.expires_at,
        )

    def _restore_from_persistent(self, scope: str, identifier: str, key: str) -> Any | None:
        persistence_agent_id = self._persistence_agent_id(scope, identifier)
        row = self.persistent.retrieve_memory_by_key(
            session_id=identifier,
            agent_id=persistence_agent_id,
            memory_type="episodic",
            key=key,
        )
        if row is None:
            return None
        self.persistent.touch_memory(row.memory_id, importance_delta=0.01)
        return row.content

    def _candidate_keys(self, query_terms: set[str], *, session_id: str | None, project_name: str | None) -> set[str]:
        candidates: set[str] = set()
        for term in query_terms:
            candidates.update(self._term_index.get(term, set()))

        if not candidates:
            candidates = set(self._hot.keys())

        if session_id:
            candidates &= self._session_index.get(session_id, set())

        if project_name:
            candidates &= self._project_index.get(project_name, set())

        return candidates

    def _add_indexes(self, full_key: str, entry: HotEntry) -> None:
        for term in entry.indexed_terms:
            self._term_index.setdefault(term, set()).add(full_key)

        if entry.scope == "session":
            self._session_index.setdefault(entry.identifier, set()).add(full_key)

        project = self._project_from_key(full_key)
        if project:
            self._project_index.setdefault(project, set()).add(full_key)

    def _remove_indexes(self, full_key: str, entry: HotEntry) -> None:
        for term in entry.indexed_terms:
            bucket = self._term_index.get(term)
            if not bucket:
                continue
            bucket.discard(full_key)
            if not bucket:
                self._term_index.pop(term, None)

        if entry.scope == "session":
            sess_bucket = self._session_index.get(entry.identifier)
            if sess_bucket:
                sess_bucket.discard(full_key)
                if not sess_bucket:
                    self._session_index.pop(entry.identifier, None)

        project = self._project_from_key(full_key)
        if project:
            prj_bucket = self._project_index.get(project)
            if prj_bucket:
                prj_bucket.discard(full_key)
                if not prj_bucket:
                    self._project_index.pop(project, None)

    def _drop_key(self, full_key: str) -> None:
        entry = self._hot.pop(full_key, None)
        if entry:
            self._remove_indexes(full_key, entry)
        self.backend.delete(full_key)

    @staticmethod
    def _project_from_key(full_key: str) -> str | None:
        parts = full_key.split(":")
        if len(parts) >= 4 and parts[0] == "memory" and parts[1] == "domain":
            return parts[2]
        return None

    def _entry_terms(self, full_key: str, value: Any) -> set[str]:
        tokens = self._tokenize(full_key)
        tokens.extend(self._tokenize(str(value)[:1024]))
        return set(tokens)

    @staticmethod
    def _tokenize(text: str) -> list[str]:
        normalized = text.lower().replace("\n", " ").replace(":", " ").replace("_", " ").replace("-", " ")
        return [part for part in normalized.split(" ") if part]

    @staticmethod
    def _semantic_similarity(query_terms: set[str], candidate_terms: list[str]) -> float:
        if not query_terms or not candidate_terms:
            return 0.0
        cset = set(candidate_terms)
        intersection = len(query_terms.intersection(cset))
        union = max(1, len(query_terms.union(cset)))
        return intersection / union
