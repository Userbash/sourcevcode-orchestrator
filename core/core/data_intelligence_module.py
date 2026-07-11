from __future__ import annotations

import re
from collections import Counter
from typing import Any


_TOKEN_RE = re.compile(r"[A-Za-zА-Яа-я0-9_\-/]+", re.UNICODE)
_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?])\s+|\n+")
_WHITESPACE_RE = re.compile(r"\s+")


class DataIntelligenceModule:
    name = "data_intelligence"

    def __init__(self) -> None:
        self._api = None
        self._hybrid_memory = None
        self._persistent_memory = None
        self._local_llm = None
        self._active = False
        self._last_state: dict[str, Any] = {}

    async def on_load(self, api) -> None:
        self._api = api
        self._active = True
        session_memory = api.get_context("session_memory") if api else None
        self._hybrid_memory = getattr(session_memory, "hybrid", None)
        self._persistent_memory = getattr(self._hybrid_memory, "persistent", None)
        module_manager = api.get_context("module_manager") if api else None
        if module_manager and hasattr(module_manager, "get_module"):
            try:
                self._local_llm = module_manager.get_module("local_llm")
            except Exception:
                self._local_llm = None

    async def on_unload(self) -> None:
        self._active = False
        self._api = None
        self._hybrid_memory = None
        self._persistent_memory = None
        self._local_llm = None

    def before_task(self, task, context: dict[str, Any] | None) -> None:
        if not self._active:
            return
        if context is None:
            context = {}
        description = getattr(getattr(task, "input", None), "description", "") or ""
        if not description.strip():
            return

        matrix = self._build_matrix(description)
        pool = self._build_prompt_pool(task, matrix)
        self._persist_artifacts(task, matrix, pool)

        context["data_intelligence"] = matrix
        context["prompt_data_pool"] = pool

        routing_hints = getattr(task, "routing_hints", None)
        if isinstance(routing_hints, dict):
            routing_hints["data_intelligence"] = {
                "keywords": matrix["keywords"][:12],
                "phrases": matrix["phrases"][:10],
                "template_count": len(matrix["templates"]),
                "sentence_count": len(matrix["sentences"]),
                "pool_hits": len(pool.get("related_memories", [])),
                "generated_text_available": bool(pool.get("generated_text")),
            }

        self._last_state = {
            "task_id": getattr(task, "id", None),
            "keywords": len(matrix["keywords"]),
            "phrases": len(matrix["phrases"]),
            "templates": len(matrix["templates"]),
            "related_memories": len(pool.get("related_memories", [])),
        }
        self._publish_event(task, matrix, pool)

    def after_task(self, task, result, context: dict[str, Any] | None) -> None:
        return None

    def finalize(self) -> dict[str, Any]:
        return {
            "active": self._active,
            "last_state": dict(self._last_state),
        }

    def _build_matrix(self, text: str) -> dict[str, Any]:
        normalized = _WHITESPACE_RE.sub(" ", text).strip()
        raw_tokens = [token.lower() for token in _TOKEN_RE.findall(normalized)]
        tokens = [token for token in raw_tokens if len(token) > 1]
        token_counter = Counter(tokens)
        keywords = [token for token, _ in token_counter.most_common(24)]
        enriched_keywords = list(dict.fromkeys(keywords + self._generate_embedding_keywords(normalized)))
        phrases = self._extract_phrases(tokens)
        sentences = self._extract_sentences(text)
        sentence_links = self._link_sentences(sentences)
        templates = self._extract_templates(text)
        char_matrix = self._build_char_matrix(normalized)
        generated_text = self._generate_text(enriched_keywords, phrases, templates, sentences)
        return {
            "normalized_text": normalized,
            "tokens": tokens,
            "token_frequency": dict(token_counter.most_common(48)),
            "keywords": enriched_keywords[:32],
            "phrases": phrases[:24],
            "sentences": sentences,
            "sentence_links": sentence_links,
            "templates": templates,
            "char_matrix": char_matrix,
            "generated_text": generated_text,
        }

    def _extract_sentences(self, text: str) -> list[str]:
        sentences = []
        for part in _SENTENCE_SPLIT_RE.split(text.strip()):
            cleaned = _WHITESPACE_RE.sub(" ", part).strip(" -\t\r\n")
            if cleaned:
                sentences.append(cleaned)
        return sentences[:24]

    def _extract_phrases(self, tokens: list[str]) -> list[str]:
        phrases: Counter[str] = Counter()
        limit = min(len(tokens), 96)
        for idx in range(limit):
            if idx + 1 < limit:
                phrases[f"{tokens[idx]} {tokens[idx + 1]}"] += 1
            if idx + 2 < limit:
                phrases[f"{tokens[idx]} {tokens[idx + 1]} {tokens[idx + 2]}"] += 1
        return [phrase for phrase, count in phrases.most_common(24) if count >= 1]

    def _link_sentences(self, sentences: list[str]) -> list[dict[str, Any]]:
        links: list[dict[str, Any]] = []
        for idx in range(len(sentences) - 1):
            left = {token.lower() for token in _TOKEN_RE.findall(sentences[idx]) if len(token) > 2}
            right = {token.lower() for token in _TOKEN_RE.findall(sentences[idx + 1]) if len(token) > 2}
            overlap = sorted(left & right)
            if overlap:
                links.append({
                    "from": idx,
                    "to": idx + 1,
                    "shared_terms": overlap[:8],
                })
        return links[:24]

    def _extract_templates(self, text: str) -> list[dict[str, Any]]:
        templates: list[dict[str, Any]] = []
        for line in text.splitlines():
            stripped = line.strip()
            if not stripped:
                continue
            if ":" in stripped and len(stripped.split(":", 1)[0]) <= 40:
                key, value = stripped.split(":", 1)
                templates.append({
                    "type": "key_value",
                    "key": key.strip().lower(),
                    "value": value.strip(),
                })
            elif "|" in stripped:
                cells = [cell.strip() for cell in stripped.split("|") if cell.strip()]
                if len(cells) >= 2:
                    templates.append({
                        "type": "table_row",
                        "cells": cells[:8],
                    })
        return templates[:24]

    def _build_char_matrix(self, text: str) -> dict[str, list[str]]:
        compact = text.lower().replace(" ", "")
        bi = Counter(compact[idx:idx + 2] for idx in range(max(len(compact) - 1, 0)) if compact[idx:idx + 2].strip())
        tri = Counter(compact[idx:idx + 3] for idx in range(max(len(compact) - 2, 0)) if compact[idx:idx + 3].strip())
        return {
            "bigrams": [chunk for chunk, _ in bi.most_common(20)],
            "trigrams": [chunk for chunk, _ in tri.most_common(20)],
        }

    def _generate_embedding_keywords(self, text: str) -> list[str]:
        if not self._local_llm or not hasattr(self._local_llm, "generate_embedding_keywords"):
            return []
        try:
            generated = self._local_llm.generate_embedding_keywords(text)
        except Exception:
            return []
        if not isinstance(generated, list):
            return []
        return [str(item).strip().lower() for item in generated if str(item).strip()]

    def _generate_text(
        self,
        keywords: list[str],
        phrases: list[str],
        templates: list[dict[str, Any]],
        sentences: list[str],
    ) -> str:
        parts: list[str] = []
        if keywords:
            parts.append("Key focus: " + ", ".join(keywords[:8]) + ".")
        if phrases:
            parts.append("Recurring phrases: " + "; ".join(phrases[:4]) + ".")
        if templates:
            template_keys = []
            for item in templates[:4]:
                if item.get("type") == "key_value":
                    template_keys.append(item.get("key", "field"))
                else:
                    template_keys.append("table")
            parts.append("Structured fragments: " + ", ".join(template_keys) + ".")
        if sentences:
            parts.append("Context summary: " + " ".join(sentences[:2]))
        return " ".join(parts).strip()

    def _build_prompt_pool(self, task, matrix: dict[str, Any]) -> dict[str, Any]:
        related_memories = self._retrieve_related_memories(task, matrix["keywords"])
        merged_keywords = list(matrix["keywords"])
        merged_phrases = list(matrix["phrases"])
        merged_templates = list(matrix["templates"])
        for item in related_memories:
            merged_keywords.extend(item.get("keywords", []))
            merged_phrases.extend(item.get("phrases", []))
            merged_templates.extend(item.get("templates", []))
        return {
            "keywords": list(dict.fromkeys(merged_keywords))[:48],
            "phrases": list(dict.fromkeys(merged_phrases))[:32],
            "templates": merged_templates[:24],
            "generated_text": matrix.get("generated_text", ""),
            "related_memories": related_memories,
        }

    def _retrieve_related_memories(self, task, keywords: list[str]) -> list[dict[str, Any]]:
        if not self._persistent_memory or not hasattr(self._persistent_memory, "retrieve_memories"):
            return []
        session_id = getattr(task, "session_id", "default") or "default"
        try:
            records = self._persistent_memory.retrieve_memories(
                session_id=session_id,
                agent_id="system:data_intelligence",
                memory_type="analytics_matrix",
                top_k=6,
            )
        except Exception:
            return []
        scored: list[tuple[int, dict[str, Any]]] = []
        keyword_set = set(keywords)
        for record in records:
            content = getattr(record, "content", None)
            if not isinstance(content, dict):
                continue
            overlap = len(keyword_set & set(content.get("keywords", [])))
            if overlap:
                scored.append((overlap, content))
        scored.sort(key=lambda item: item[0], reverse=True)
        return [content for _, content in scored[:4]]

    def _persist_artifacts(self, task, matrix: dict[str, Any], pool: dict[str, Any]) -> None:
        session_id = getattr(task, "session_id", "default") or "default"
        task_id = getattr(task, "id", None) or "unknown"
        if self._hybrid_memory and hasattr(self._hybrid_memory, "set_by_full_key"):
            try:
                self._hybrid_memory.set_by_full_key(
                    f"data_intelligence:{session_id}:{task_id}",
                    {
                        "keywords": matrix["keywords"],
                        "phrases": matrix["phrases"],
                        "templates": matrix["templates"],
                        "generated_text": matrix["generated_text"],
                    },
                    memory_type="episodic",
                    tags=["data-intelligence", "prompt-pool", *matrix["keywords"][:6]],
                )
            except Exception:
                pass
        if self._persistent_memory and hasattr(self._persistent_memory, "store_memory"):
            try:
                self._persistent_memory.store_memory(
                    session_id=session_id,
                    agent_id="system:data_intelligence",
                    memory_type="analytics_matrix",
                    content={
                        "task_id": task_id,
                        "keywords": matrix["keywords"],
                        "phrases": matrix["phrases"],
                        "templates": matrix["templates"],
                        "sentence_links": matrix["sentence_links"],
                        "char_matrix": matrix["char_matrix"],
                        "generated_text": pool.get("generated_text", ""),
                    },
                    importance=0.55,
                    tags=["data-intelligence", "analytics-matrix"],
                )
            except Exception:
                pass

    def _publish_event(self, task, matrix: dict[str, Any], pool: dict[str, Any]) -> None:
        if not self._api:
            return
        hub = getattr(self._api, "runtime_event_stream_hub", None)
        if not hub or not hasattr(hub, "publish_agent_event"):
            return
        try:
            hub.publish_agent_event(
                {
                    "agent": self.name,
                    "task_id": getattr(task, "id", None),
                    "keywords": matrix["keywords"][:8],
                    "phrases": matrix["phrases"][:6],
                    "related_memories": len(pool.get("related_memories", [])),
                }
            )
        except Exception:
            return None
