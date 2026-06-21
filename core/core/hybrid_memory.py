from __future__ import annotations

import asyncio
import hashlib
import logging
import re
from collections import Counter
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from collections.abc import Callable
from typing import Any
import math

from .memory_backend import BackendEntry, InMemoryBackend, MemoryBackend
from .memory_settings import MemorySettings
from .persistent_memory import PersistentMemoryManager
from .model_value import memory_efficiency_score

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
        self._event_publisher: Callable[[str, dict[str, Any]], None] | None = None
        self._retrieval_min_score = max(0.0, min(1.0, float(getattr(self.settings, "retrieval_min_score", 0.34) or 0.34)))
        self._retrieval_min_semantic_similarity = max(0.0, min(1.0, float(getattr(self.settings, "retrieval_min_semantic_similarity", 0.16) or 0.16)))
        self._retrieval_min_vector_similarity = max(0.0, min(1.0, float(getattr(self.settings, "retrieval_min_vector_similarity", 0.10) or 0.10)))
        self._retrieval_min_summary_signal = max(0.0, min(1.0, float(getattr(self.settings, "retrieval_min_summary_signal", 0.18) or 0.18)))
        self._retrieval_reuse_min_score = max(0.0, min(1.0, float(getattr(self.settings, "retrieval_reuse_min_score", 0.48) or 0.48)))

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


    def set_event_publisher(self, publisher: Callable[[str, dict[str, Any]], None] | None) -> None:
        self._event_publisher = publisher
        if hasattr(self.persistent, "set_event_publisher"):
            self.persistent.set_event_publisher(publisher)

    def attach_event_bus(self, message_bus: Any | None) -> None:
        if message_bus is None or not hasattr(message_bus, "publish"):
            self.set_event_publisher(None)
            return
        self.set_event_publisher(lambda topic, payload: message_bus.publish(topic, payload))

    def _emit_event(self, topic: str, payload: dict[str, Any]) -> None:
        if self._event_publisher is None:
            return
        try:
            self._event_publisher(topic, payload)
        except Exception as exc:
            logger.warning("[MEMORY] event publish failed for %s: %s", topic, exc)

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
        hot_capacity = int(getattr(self.settings, "hot_cache_max_entries", 0) or 0)
        efficiency = memory_efficiency_score(
            memory_context_bytes=0,
            context_window=max(1, hot_capacity * 1024),
            memory_keys_count=len(hot_keys),
            hot_count=len(hot_keys),
            hot_capacity=hot_capacity,
            persistent_enabled=persistent_enabled,
        )
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
            "hot_capacity": hot_capacity,
            "memory_efficiency_score": efficiency,
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

        # Keep the local-LM hook intact, but retrieval quality should not depend on it.
        if api:
            local_llm = api.get_module("local_llm")
            if local_llm and getattr(local_llm, "ready", False):
                try:
                    pass
                except Exception:
                    pass

        norm_query_terms = {term for term in self._tokenize(query_text) if len(term) >= 2}
        query_vector = self._hashed_vector(query_text)
        candidate_keys = self._candidate_keys(norm_query_terms, session_id=session_id, project_name=project_name)

        for full_key in candidate_keys:
            entry = self._hot.get(full_key)
            if not entry:
                continue

            candidate_text = self._semantic_text_for_entry(entry)
            candidate_terms = self._tokenize(candidate_text)
            term_similarity = self._semantic_similarity(norm_query_terms, candidate_terms)
            vector_similarity = max(0.0, self._cosine_similarity(query_vector, self._hashed_vector(candidate_text)))
            summary_signal = self._summary_signal_score(candidate_text)
            exact_key_match = 1.0 if any(part in full_key.lower() for part in norm_query_terms if len(part) >= 4) else 0.0
            semantic_similarity = (
                term_similarity * 0.5
                + vector_similarity * 0.35
                + min(1.0, summary_signal) * 0.1
                + exact_key_match * 0.05
            )
            age_sec = max(1.0, (now - entry.last_accessed).total_seconds())
            time_decay = 1.0 / (1.0 + age_sec / 3600.0)

            score = (
                0.58 * semantic_similarity
                + 0.22 * entry.importance_score
                + 0.15 * time_decay
                + 0.05 * min(1.0, entry.access_count / 4.0)
            )
            if semantic_similarity < self._retrieval_min_semantic_similarity:
                continue
            if vector_similarity < self._retrieval_min_vector_similarity and term_similarity < (self._retrieval_min_semantic_similarity + 0.08):
                continue
            if summary_signal < self._retrieval_min_summary_signal and term_similarity < 0.35 and exact_key_match <= 0.0:
                continue
            if score < self._retrieval_min_score:
                continue

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

    def _trained_cache_key(self, *, session_id: str, agent_id: str, memory_domain: str, top_k: int, token_limit: int, quality_threshold: float, query_signature: str = '') -> tuple[str, str, str, int, int, float, str]:
        return (session_id, agent_id, memory_domain, int(top_k), int(token_limit), round(float(quality_threshold), 3), query_signature[:24])

    def _trained_cache_get(self, *, session_id: str, agent_id: str, memory_domain: str, top_k: int, token_limit: int, quality_threshold: float, query_signature: str = '') -> str | None:
        key = self._trained_cache_key(session_id=session_id, agent_id=agent_id, memory_domain=memory_domain, top_k=top_k, token_limit=token_limit, quality_threshold=quality_threshold, query_signature=query_signature)
        cached = self._trained_memory_brief_cache.get(key)
        if not cached:
            return None
        expires_at, brief = cached
        if datetime.now(UTC) >= expires_at:
            self._trained_memory_brief_cache.pop(key, None)
            return None
        return brief

    def _trained_cache_set(self, *, session_id: str, agent_id: str, memory_domain: str, top_k: int, token_limit: int, quality_threshold: float, brief: str, query_signature: str = '') -> None:
        key = self._trained_cache_key(session_id=session_id, agent_id=agent_id, memory_domain=memory_domain, top_k=top_k, token_limit=token_limit, quality_threshold=quality_threshold, query_signature=query_signature)
        self._trained_memory_brief_cache[key] = (datetime.now(UTC) + timedelta(seconds=self._trained_memory_brief_ttl_sec), brief)

    def _trained_memory_rank(self, record: Any, *, position: int) -> float:
        if isinstance(record, dict):
            score = float(record.get("quality_score", 0.0) or 0.0)
            created_at = record.get("created_at", "")
        else:
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


    @staticmethod
    def _trained_memory_payload(record: Any) -> tuple[dict[str, Any], dict[str, Any], float, str, list[Any], str | None]:
        if isinstance(record, dict):
            content = record.get("content")
            metadata = dict(record.get("metadata") or {})
            quality = float(record.get("quality_score", 0.0) or 0.0)
            created_at = record.get("created_at")
            source_ids = list(record.get("source_memory_ids") or [])
            domain = str(record.get("memory_domain", "") or "")
        else:
            content = getattr(record, "content", None)
            metadata = dict(getattr(record, "metadata", {}) or {})
            quality = float(getattr(record, "quality_score", 0.0) or 0.0)
            created_at = getattr(record, "created_at", None)
            source_ids = list(getattr(record, "source_memory_ids", []) or [])
            domain = str(getattr(record, "memory_domain", "") or "")
        payload = content if isinstance(content, dict) else {"summary": str(content or "")}
        return payload, metadata, quality, domain, source_ids, created_at

    def _trained_memory_document(self, record: Any, *, memory_domain: str, query_text: str = "", files: list[str] | None = None, constraints: list[str] | None = None, acceptance_criteria: list[str] | None = None) -> tuple[str, float]:
        payload, metadata, quality, domain, _, created_at = self._trained_memory_payload(record)
        parts = [
            str(metadata.get("semantic_document") or "").strip(),
            str(payload.get("problem") or payload.get("objective") or "").strip(),
            str(payload.get("summary") or "").strip(),
            str(payload.get("outcome") or "").strip(),
            str(payload.get("failure_mode") or "").strip(),
            " ".join(str(item) for item in (payload.get("files") or [])),
            " ".join(str(item) for item in (payload.get("constraints") or [])),
            " ".join(str(item) for item in (payload.get("acceptance_criteria") or [])),
            str(payload.get("reuse_hint") or "").strip(),
            domain or memory_domain,
        ]
        document = "\n".join(part for part in parts if part)
        summary_signal = self._summary_signal_score(document)
        age_bonus = 0.0
        if created_at:
            try:
                age_dt = datetime.fromisoformat(str(created_at).replace("Z", "+00:00"))
                age_hours = max(0.0, (datetime.now(UTC) - age_dt).total_seconds() / 3600.0)
                age_bonus = 1.0 / (1.0 + age_hours / 48.0)
            except Exception:
                age_bonus = 0.0
        richness = max(0.0, min(1.0, summary_signal * 0.55 + quality * 0.3 + age_bonus * 0.15))
        return document, richness

    def _trained_query_signature(self, *, memory_domain: str, query_text: str = "", files: list[str] | None = None, constraints: list[str] | None = None, acceptance_criteria: list[str] | None = None) -> str:
        blob = "\n".join([
            memory_domain,
            query_text or "",
            " ".join(files or []),
            " ".join(constraints or []),
            " ".join(acceptance_criteria or []),
        ])
        if not blob.strip():
            return ""
        return hashlib.sha1(blob.encode("utf-8")).hexdigest()[:24]

    def _degrade_key(self, session_id: str, task_type: str) -> tuple[str, str]:
        return (session_id, task_type)

    def record_trained_memory_rejection(self, *, session_id: str, task_type: str, threshold: float, reason: str) -> None:
        key = self._degrade_key(session_id, task_type)
        self._trained_memory_degrade[key] = datetime.now(UTC) + timedelta(seconds=self._trained_memory_degrade_ttl_sec)
        logger.info("[MEMORY] trained memory rejected session=%s task_type=%s threshold=%.2f reason=%s", session_id, task_type, threshold, reason)
        self._emit_event("memory.trained.events", {
            "event_type": "memory.trained.rejected",
            "session_id": session_id,
            "task_type": task_type,
            "threshold": float(threshold),
            "reason": reason,
        })

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
        self._emit_event("memory.trained.events", {
            "event_type": "memory.trained.outcome",
            "session_id": session_id,
            "task_type": task_type,
            "accepted": bool(accepted),
            "threshold": float(threshold),
            "reason": reason,
            "accepted_count": stats["accepted"],
            "rejected_count": stats["rejected"],
            "rejection_rate": rejection_rate,
        })
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

    def _rank_trained_memories(
        self,
        records: list[Any],
        *,
        top_k: int,
        quality_threshold: float,
        memory_domain: str,
        query_text: str = "",
        files: list[str] | None = None,
        constraints: list[str] | None = None,
        acceptance_criteria: list[str] | None = None,
    ) -> list[Any]:
        filtered = []
        query_document = "\n".join([
            memory_domain,
            query_text or memory_domain,
            " ".join(files or []),
            " ".join(constraints or []),
            " ".join(acceptance_criteria or []),
        ])
        query_terms = set(self._tokenize(query_document))
        query_vector = self._hashed_vector(query_document)
        for position, record in enumerate(records):
            payload, metadata, quality, domain, _, _ = self._trained_memory_payload(record)
            if quality < quality_threshold:
                continue
            document, richness = self._trained_memory_document(
                record,
                memory_domain=memory_domain,
                query_text=query_text,
                files=files,
                constraints=constraints,
                acceptance_criteria=acceptance_criteria,
            )
            if richness < self._retrieval_min_summary_signal:
                continue
            record_terms = set(self._tokenize(document))
            term_similarity = self._semantic_similarity(query_terms, list(record_terms))
            vector = metadata.get("semantic_vector")
            if not isinstance(vector, list) or not vector:
                vector = self._hashed_vector(document)
            vector_similarity = max(0.0, self._cosine_similarity(query_vector, [float(item) for item in vector]))
            reuse_bonus = 0.05 if str(payload.get("reuse_hint") or "").strip() else 0.0
            score = quality * 0.32 + richness * 0.23 + term_similarity * 0.2 + vector_similarity * 0.18 + reuse_bonus + self._trained_memory_rank(record, position=position) * 0.07
            if term_similarity < self._retrieval_min_semantic_similarity and vector_similarity < self._retrieval_min_vector_similarity:
                continue
            filtered.append((score, record, domain or memory_domain))
        filtered.sort(key=lambda item: item[0], reverse=True)
        return [record for _, record, _ in filtered[:max(1, int(top_k))]]

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
        query_text: str = '',
        files: list[str] | None = None,
        constraints: list[str] | None = None,
        acceptance_criteria: list[str] | None = None,
    ) -> str:
        normalized_task_type = str(task_type or memory_domain.split(":", 1)[-1]).lower()
        if not allow_trained_memory or self._trained_memory_degraded(session_id=session_id, task_type=normalized_task_type):
            return ""
        quality_threshold = self._trained_quality_threshold_for_domain(memory_domain)
        query_signature = self._trained_query_signature(memory_domain=memory_domain, query_text=query_text, files=files, constraints=constraints, acceptance_criteria=acceptance_criteria)
        cached = self._trained_cache_get(
            session_id=session_id,
            agent_id=agent_id,
            memory_domain=memory_domain,
            top_k=top_k,
            token_limit=token_limit,
            quality_threshold=quality_threshold,
            query_signature=query_signature,
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
        records = self._rank_trained_memories(records, top_k=top_k, quality_threshold=quality_threshold, memory_domain=memory_domain, query_text=query_text, files=files, constraints=constraints, acceptance_criteria=acceptance_criteria)
        if not records:
            self.record_trained_memory_outcome(session_id=session_id, task_type=normalized_task_type, accepted=False, threshold=quality_threshold, reason="quality_threshold")
            return ""

        lines = [f"--- TRAINED MEMORY BRIEF ({memory_domain}, Top {len(records)}) ---"]
        used = len(lines[0])
        budget_chars = max(200, token_limit * 4)
        for record in records:
            payload_dict, metadata, score, domain, source_ids, _ = self._trained_memory_payload(record)
            label = f"[Quality: {score:.2f}] [Domain: {domain or memory_domain}] [Sources: {source_ids}]"
            payload = str(payload_dict.get('summary') or payload_dict.get('outcome') or payload_dict.get('problem') or payload_dict)
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
            query_signature=query_signature,
        )
        return brief

    def get_trained_memory_context(
        self,
        *,
        session_id: str,
        agent_id: str,
        memory_domain: str,
        top_k: int = 3,
        query_text: str = '',
        files: list[str] | None = None,
        constraints: list[str] | None = None,
        acceptance_criteria: list[str] | None = None,
    ) -> dict[str, Any]:
        normalized_task_type = str(memory_domain.split(":", 1)[-1]).lower()
        quality_threshold = self._trained_quality_threshold_for_domain(memory_domain)
        brief = self.retrieve_trained_memory_brief(
            session_id=session_id,
            agent_id=agent_id,
            memory_domain=memory_domain,
            top_k=top_k,
            query_text=query_text,
            files=files,
            constraints=constraints,
            acceptance_criteria=acceptance_criteria,
        )
        records: list[Any] = []
        if brief and hasattr(self.persistent, "retrieve_trained_memories"):
            try:
                records = self.persistent.retrieve_trained_memories(
                    session_id=session_id,
                    agent_id=agent_id,
                    memory_domain=memory_domain,
                    top_k=max(1, int(top_k)) * 3,
                )
            except Exception:
                records = []
        ranked = self._rank_trained_memories(records, top_k=top_k, quality_threshold=quality_threshold, memory_domain=memory_domain, query_text=query_text, files=files, constraints=constraints, acceptance_criteria=acceptance_criteria) if records else []
        provenance: list[Any] = []
        confidence_score = 0.0
        age_sec: float | None = None
        if ranked:
            source_ids: list[Any] = []
            scores: list[float] = []
            ages: list[float] = []
            now = datetime.now(UTC)
            for record in ranked:
                if isinstance(record, dict):
                    source_ids.extend(list(record.get("source_memory_ids") or []))
                    scores.append(float(record.get("quality_score", 0.0) or 0.0))
                    created_at = record.get("created_at")
                else:
                    source_ids.extend(list(getattr(record, "source_memory_ids", []) or []))
                    scores.append(float(getattr(record, "quality_score", 0.0) or 0.0))
                    created_at = getattr(record, "created_at", None)
                if created_at:
                    try:
                        ts = datetime.fromisoformat(str(created_at).replace("Z", "+00:00"))
                    except Exception:
                        ts = None
                    if ts is not None:
                        ages.append(max(0.0, (now - ts).total_seconds()))
            provenance = source_ids or [f"trained_memory:{memory_domain}"]
            confidence_score = round(sum(scores) / len(scores), 4) if scores else 0.0
            age_sec = min(ages) if ages else None
        return {
            "brief": brief,
            "memory_domain": memory_domain,
            "session_id": session_id,
            "agent_id": agent_id,
            "has_trained_memory": bool(brief),
            "provenance": provenance,
            "confidence_score": confidence_score,
            "age_sec": age_sec,
            "quality_threshold": quality_threshold,
            "task_type": normalized_task_type,
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

    @staticmethod
    def _normalize_reuse_text(value: Any) -> str:
        text = str(value or "").strip().lower()
        text = re.sub(r"[^a-z0-9_./:#\-\s]+", " ", text)
        text = re.sub(r"\s+", " ", text)
        return text.strip()

    @staticmethod
    def _summary_signal_score(text: Any) -> float:
        normalized = HybridMemory._normalize_reuse_text(text)
        if not normalized:
            return 0.0
        tokens = [token for token in normalized.split() if token]
        if not tokens:
            return 0.0
        unique_ratio = len(set(tokens)) / len(tokens)
        informative_ratio = sum(1 for token in tokens if len(token) >= 4) / len(tokens)
        alpha_ratio = sum(1 for char in normalized if char.isalnum()) / max(1, len(normalized))
        length_score = min(1.0, len(tokens) / 14.0)
        return max(0.0, min(1.0, 0.3 * length_score + 0.3 * unique_ratio + 0.25 * informative_ratio + 0.15 * alpha_ratio))

    def _semantic_text_for_entry(self, entry: HotEntry) -> str:
        parts = [entry.key, " ".join(entry.tags), str(entry.value)[:1200]]
        if entry.stub and entry.stub.summary:
            parts.append(entry.stub.summary[:240])
        return "\n".join(part for part in parts if part)

    def _hashed_vector(self, text: Any, *, dims: int | None = None) -> list[float]:
        normalized = self._normalize_reuse_text(text)
        width = max(8, int(dims or self.settings.semantic_vector_dims or 48))
        vector = [0.0] * width
        if not normalized:
            return vector
        tokens = normalized.split()
        counts = Counter(tokens)
        for token, count in counts.items():
            digest = hashlib.sha256(token.encode("utf-8")).digest()
            idx = int.from_bytes(digest[:2], "big") % width
            sign = 1.0 if digest[2] % 2 == 0 else -1.0
            weight = (1.0 + min(2.0, len(token) / 12.0)) * (1.0 + min(1.5, math.log1p(count)))
            vector[idx] += sign * weight
        compact = normalized.replace(" ", "")
        if len(compact) >= 3:
            for start in range(0, len(compact) - 2):
                trigram = compact[start:start + 3]
                digest = hashlib.sha256(f"tri:{trigram}".encode("utf-8")).digest()
                idx = int.from_bytes(digest[:2], "big") % width
                sign = 1.0 if digest[2] % 2 == 0 else -1.0
                vector[idx] += sign * 0.35
        norm = math.sqrt(sum(value * value for value in vector))
        if norm <= 1e-9:
            return vector
        return [round(value / norm, 6) for value in vector]

    @staticmethod
    def _cosine_similarity(left: list[float], right: list[float]) -> float:
        if not left or not right or len(left) != len(right):
            return 0.0
        dot = sum(a * b for a, b in zip(left, right))
        left_norm = math.sqrt(sum(a * a for a in left))
        right_norm = math.sqrt(sum(b * b for b in right))
        if left_norm <= 1e-9 or right_norm <= 1e-9:
            return 0.0
        return max(-1.0, min(1.0, dot / (left_norm * right_norm)))

    def _reuse_terms(self, *, objective: str, files: list[str], constraints: list[str], acceptance_criteria: list[str]) -> list[str]:
        buckets = [objective, *files, *constraints, *acceptance_criteria]
        terms: list[str] = []
        for raw in buckets:
            normalized = self._normalize_reuse_text(raw)
            if not normalized:
                continue
            for token in normalized.split():
                if len(token) < 3:
                    continue
                if token not in terms:
                    terms.append(token)
        return terms[:48]

    def build_task_reuse_fingerprint(
        self,
        *,
        task_type: str,
        objective: str,
        files: list[str],
        constraints: list[str],
        acceptance_criteria: list[str],
    ) -> tuple[str, list[str]]:
        terms = self._reuse_terms(
            objective=objective,
            files=files,
            constraints=constraints,
            acceptance_criteria=acceptance_criteria,
        )
        digest_source = "|".join([task_type.lower(), *terms])
        fingerprint = hashlib.sha1(digest_source.encode("utf-8")).hexdigest()[:16]
        return fingerprint, terms

    def store_reusable_task_memory(
        self,
        *,
        task: Any,
        agent_id: str,
        summary: str,
        quality_score: float = 0.0,
        provider: str | None = None,
        model_name: str | None = None,
    ) -> str:
        task_type = str(getattr(getattr(task, "type", None), "value", getattr(task, "type", "unknown")) or "unknown").lower()
        description = str(getattr(getattr(task, "input", None), "description", "") or "")
        files = list(getattr(getattr(task, "input", None), "files", []) or [])
        constraints = list(getattr(getattr(task, "input", None), "constraints", []) or [])
        acceptance_criteria = list(getattr(getattr(task, "input", None), "acceptance_criteria", []) or [])
        capability = str(getattr(task, "required_capability", "") or task_type)
        project = str(getattr(getattr(task, "context", None), "project", "") or "")
        branch = str(getattr(getattr(task, "context", None), "branch", "") or "")
        session_id = str(getattr(task, "session_id", "") or getattr(task, "task_id", "default"))
        fingerprint, fingerprint_terms = self.build_task_reuse_fingerprint(
            task_type=task_type,
            objective=description,
            files=files,
            constraints=constraints,
            acceptance_criteria=acceptance_criteria,
        )
        payload = {
            "task_type": task_type,
            "task_id": str(getattr(task, "task_id", "")),
            "summary": str(summary or "")[:1200],
            "objective": description[:400],
            "files": files[:12],
            "constraints": constraints[:12],
            "acceptance_criteria": acceptance_criteria[:12],
            "fingerprint": fingerprint,
            "fingerprint_terms": fingerprint_terms,
            "capability": capability,
            "project": project,
            "branch": branch,
            "provider": str(provider or ""),
            "model_name": str(model_name or ""),
            "source_agent_id": agent_id,
        }
        metadata = {
            "source": "reusable_task_memory",
            "task_type": task_type,
            "fingerprint": fingerprint,
            "fingerprint_terms": fingerprint_terms,
            "capability": capability,
            "project": project,
            "branch": branch,
            "provider": str(provider or ""),
            "model_name": str(model_name or ""),
            "source_agent_id": agent_id,
        }
        shared_agent_id = f"shared:{capability}"
        self.persistent.store_trained_memory(
            session_id=session_id,
            agent_id=shared_agent_id,
            memory_domain=f"reuse:{task_type}",
            content=payload,
            source_memory_ids=[],
            metadata=metadata,
            quality_score=max(0.0, min(1.0, float(quality_score) or 0.0)),
        )
        self._emit_event("memory.reuse.events", {
            "event_type": "memory.reuse.stored",
            "session_id": session_id,
            "task_type": task_type,
            "fingerprint": fingerprint,
            "capability": capability,
            "project": project,
            "provider": str(provider or ""),
            "model_name": str(model_name or ""),
            "source_agent_id": agent_id,
        })
        return fingerprint

    def retrieve_reusable_task_context(
        self,
        *,
        task: Any,
        agent_id: str | None = None,
        capability: str | None = None,
        top_k: int = 2,
        token_limit: int = 220,
    ) -> dict[str, Any]:
        task_type = str(getattr(getattr(task, "type", None), "value", getattr(task, "type", "unknown")) or "unknown").lower()
        description = str(getattr(getattr(task, "input", None), "description", "") or "")
        files = list(getattr(getattr(task, "input", None), "files", []) or [])
        constraints = list(getattr(getattr(task, "input", None), "constraints", []) or [])
        acceptance_criteria = list(getattr(getattr(task, "input", None), "acceptance_criteria", []) or [])
        project = str(getattr(getattr(task, "context", None), "project", "") or "")
        capability = str(capability or getattr(task, "required_capability", "") or task_type)
        fingerprint, query_terms = self.build_task_reuse_fingerprint(
            task_type=task_type,
            objective=description,
            files=files,
            constraints=constraints,
            acceptance_criteria=acceptance_criteria,
        )
        try:
            records = self.persistent.list_trained_memories(limit=max(40, int(top_k) * 40))
        except Exception:
            records = []
        if not records:
            return {"matched": False, "brief": "", "fingerprint": fingerprint, "similarity": 0.0, "count": 0}

        query_set = set(query_terms)
        file_set = {item.lower() for item in files}
        domain = f"reuse:{task_type}"
        query_vector = self._hashed_vector(
            "\n".join([
                description,
                " ".join(str(item) for item in files),
                " ".join(str(item) for item in constraints),
                " ".join(str(item) for item in acceptance_criteria),
            ])
        )
        ranked: list[tuple[float, Any, dict[str, Any], dict[str, Any]]] = []
        for record in records:
            record_domain = str(getattr(record, "memory_domain", "") or "")
            if record_domain != domain:
                continue
            metadata = dict(getattr(record, "metadata", {}) or {})
            content = getattr(record, "content", {})
            content = content if isinstance(content, dict) else {"summary": str(content or "")}
            record_project = str(metadata.get("project") or content.get("project") or "")
            if project and record_project and record_project != project:
                continue
            record_terms = metadata.get("fingerprint_terms") or content.get("fingerprint_terms") or []
            record_terms = [str(item).lower() for item in record_terms if str(item).strip()]
            record_set = set(record_terms)
            if not record_set:
                record_set = set(self._reuse_terms(
                    objective=str(content.get("objective") or content.get("summary") or ""),
                    files=list(content.get("files") or []),
                    constraints=list(content.get("constraints") or []),
                    acceptance_criteria=list(content.get("acceptance_criteria") or []),
                ))
            if not record_set:
                continue
            shared_terms = query_set & record_set
            overlap = len(shared_terms) / max(1, len(query_set | record_set))
            query_coverage = len(shared_terms) / max(1, len(query_set))
            record_files = {str(item).lower() for item in (content.get("files") or []) if str(item).strip()}
            file_overlap = len(file_set & record_files) / max(1, len(file_set | record_files)) if file_set and record_files else 0.0
            record_capability = str(metadata.get("capability") or content.get("capability") or "")
            summary_text = str(content.get("summary") or content.get("objective") or "")
            summary_signal = self._summary_signal_score(summary_text)
            record_vector = self._hashed_vector(
                "\n".join([
                    str(content.get("objective") or ""),
                    summary_text,
                    " ".join(str(item) for item in (content.get("files") or [])),
                    " ".join(str(item) for item in (content.get("constraints") or [])),
                    " ".join(str(item) for item in (content.get("acceptance_criteria") or [])),
                ])
            )
            vector_similarity = max(0.0, self._cosine_similarity(query_vector, record_vector))
            score = overlap * 0.28 + query_coverage * 0.2 + vector_similarity * 0.22 + summary_signal * 0.08
            if file_overlap:
                score += file_overlap * 0.1
            if project and record_project == project:
                score += 0.08
            if capability and record_capability == capability:
                score += 0.08
            if str(metadata.get("fingerprint") or content.get("fingerprint") or "") == fingerprint:
                score += 0.2
            if agent_id and str(getattr(record, "agent_id", "")) == str(agent_id):
                score += 0.05
            quality_score = float(getattr(record, "quality_score", 0.0) or 0.0)
            score += min(0.09, quality_score * 0.09)
            if summary_signal < self._retrieval_min_summary_signal and overlap < 0.2 and file_overlap < 0.25:
                continue
            if vector_similarity < self._retrieval_min_vector_similarity and overlap < 0.28:
                continue
            if score < self._retrieval_reuse_min_score:
                continue
            ranked.append((score, record, metadata, content))

        ranked.sort(key=lambda item: item[0], reverse=True)
        chosen = ranked[: max(1, int(top_k))]
        if not chosen:
            return {"matched": False, "brief": "", "fingerprint": fingerprint, "similarity": 0.0, "count": 0}

        budget_chars = max(200, int(token_limit) * 4)
        lines = [f"--- REUSABLE TASK MEMORY ({task_type}, Top {len(chosen)}) ---"]
        used = len(lines[0])
        source_ids: list[int] = []
        for score, record, metadata, content in chosen:
            capability_label = str(metadata.get("capability") or content.get("capability") or "")
            source_ids.append(int(getattr(record, "trained_memory_id", 0) or 0))
            summary = str(content.get("summary") or content.get("objective") or "").strip()
            if len(summary) > 420:
                summary = summary[:417] + "..."
            line = f"[Reuse: {score:.2f}] [Capability: {capability_label or task_type}] [Sources: [{getattr(record, 'trained_memory_id', 0)}]] {summary}"
            if used + len(line) + 1 > budget_chars:
                break
            lines.append(line)
            used += len(line) + 1
        brief = "\n".join(lines)
        similarity = float(chosen[0][0])
        self._emit_event("memory.reuse.events", {
            "event_type": "memory.reuse.recalled",
            "task_type": task_type,
            "fingerprint": fingerprint,
            "similarity": similarity,
            "count": len(chosen),
            "project": project,
            "capability": capability,
            "source_ids": source_ids,
        })
        return {
            "matched": True,
            "brief": brief,
            "fingerprint": fingerprint,
            "similarity": round(similarity, 4),
            "count": len(chosen),
            "source_ids": source_ids,
            "capability": capability,
            "project": project,
        }

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
        intersection_terms = query_terms.intersection(cset)
        intersection = len(intersection_terms)
        union = max(1, len(query_terms.union(cset)))
        jaccard = intersection / union
        coverage = intersection / max(1, len(query_terms))
        informative_query_terms = {term for term in query_terms if len(term) >= 5}
        informative_hits = len(intersection_terms.intersection(informative_query_terms))
        informative_coverage = informative_hits / max(1, len(informative_query_terms)) if informative_query_terms else coverage
        return min(1.0, jaccard * 0.45 + coverage * 0.35 + informative_coverage * 0.2)
