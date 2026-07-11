from __future__ import annotations

import math
import re
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass, field
from typing import Any, Protocol


_TOKEN_RE = re.compile(r"[A-Za-zА-Яа-я0-9_\-/]+", re.UNICODE)
_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?])\s+|\n+")
_WHITESPACE_RE = re.compile(r"\s+")


class TextGenerationAdapter(Protocol):
    def generate_text(self, *, prompt: str, context: dict[str, Any] | None = None) -> str:
        ...

    def generate_embedding_keywords(self, text: str) -> list[str]:
        ...


@dataclass
class SentenceEdge:
    source: int
    target: int
    weight: float
    shared_terms: list[str] = field(default_factory=list)


@dataclass
class TemplateRecord:
    template_type: str
    values: dict[str, Any]


@dataclass
class AnalyticsMatrixReport:
    normalized_text: str
    keywords: list[str]
    keyword_scores: dict[str, float]
    token_frequency: dict[str, int]
    phrase_frequency: dict[str, int]
    token_links: dict[str, dict[str, int]]
    sentence_nodes: list[str]
    sentence_edges: list[SentenceEdge]
    templates: list[TemplateRecord]
    char_matrix: dict[str, list[str]]
    generated_text: str
    prompt_pool: dict[str, Any]
    metadata: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["templates"] = [asdict(item) for item in self.templates]
        payload["sentence_edges"] = [asdict(item) for item in self.sentence_edges]
        return payload


@dataclass
class KnowledgeRecord:
    source_id: str
    keywords: list[str]
    phrases: list[str]
    sentences: list[str]
    generated_text: str
    templates: list[dict[str, Any]]
    metadata: dict[str, Any] = field(default_factory=dict)


class AnalyticsKnowledgePool:
    def __init__(self) -> None:
        self._records: list[KnowledgeRecord] = []
        self._keyword_index: dict[str, list[int]] = defaultdict(list)

    def ingest(self, report: AnalyticsMatrixReport, *, source_id: str, metadata: dict[str, Any] | None = None) -> KnowledgeRecord:
        record = KnowledgeRecord(
            source_id=source_id,
            keywords=list(report.keywords),
            phrases=list(report.phrase_frequency.keys())[:32],
            sentences=list(report.sentence_nodes),
            generated_text=report.generated_text,
            templates=[item.values | {"template_type": item.template_type} for item in report.templates],
            metadata=dict(metadata or {}),
        )
        index = len(self._records)
        self._records.append(record)
        for keyword in record.keywords:
            if index not in self._keyword_index[keyword]:
                self._keyword_index[keyword].append(index)
        return record

    def query(self, text: str, *, top_k: int = 5) -> list[KnowledgeRecord]:
        keywords = _tokenize(text)
        scores: Counter[int] = Counter()
        for keyword in keywords:
            for index in self._keyword_index.get(keyword, []):
                scores[index] += 1
        ranked = sorted(scores.items(), key=lambda item: (-item[1], item[0]))
        return [self._records[index] for index, _ in ranked[: max(1, top_k)]]

    def snapshot(self) -> dict[str, Any]:
        return {
            "records": [asdict(item) for item in self._records],
            "record_count": len(self._records),
            "keywords": sorted(self._keyword_index.keys()),
        }


class AnalyticsMatrixEngine:
    def __init__(self, *, generator: TextGenerationAdapter | None = None) -> None:
        self.generator = generator

    def analyze(
        self,
        text: str,
        *,
        metadata: dict[str, Any] | None = None,
        knowledge_pool: AnalyticsKnowledgePool | None = None,
    ) -> AnalyticsMatrixReport:
        normalized_text = _WHITESPACE_RE.sub(" ", text).strip()
        tokens = _tokenize(normalized_text)
        token_frequency = Counter(tokens)
        keyword_scores = self._score_keywords(tokens)
        keywords = [token for token, _ in sorted(keyword_scores.items(), key=lambda item: item[1], reverse=True)[:32]]
        generated_keywords = self._generate_embedding_keywords(normalized_text)
        keywords = list(dict.fromkeys(keywords + generated_keywords))
        phrase_frequency = self._extract_phrases(tokens)
        token_links = self._link_tokens(tokens)
        sentence_nodes = self._extract_sentences(text)
        sentence_edges = self._link_sentences(sentence_nodes)
        templates = self._extract_templates(text)
        char_matrix = self._build_char_matrix(normalized_text)
        prompt_pool = self._build_prompt_pool(
            normalized_text,
            keywords=keywords,
            phrase_frequency=phrase_frequency,
            sentence_nodes=sentence_nodes,
            templates=templates,
            knowledge_pool=knowledge_pool,
        )
        generated_text = self._generate_text(
            normalized_text,
            keywords=keywords,
            phrase_frequency=phrase_frequency,
            sentence_nodes=sentence_nodes,
            templates=templates,
            prompt_pool=prompt_pool,
        )
        report = AnalyticsMatrixReport(
            normalized_text=normalized_text,
            keywords=keywords,
            keyword_scores=keyword_scores,
            token_frequency=dict(token_frequency.most_common(64)),
            phrase_frequency=dict(phrase_frequency.most_common(40)),
            token_links=token_links,
            sentence_nodes=sentence_nodes,
            sentence_edges=sentence_edges,
            templates=templates,
            char_matrix=char_matrix,
            generated_text=generated_text,
            prompt_pool=prompt_pool,
            metadata=dict(metadata or {}),
        )
        return report

    def _score_keywords(self, tokens: list[str]) -> dict[str, float]:
        frequencies = Counter(tokens)
        total = max(len(tokens), 1)
        scores: dict[str, float] = {}
        for token, count in frequencies.items():
            locality_bonus = 1.0 + min(tokens.index(token), 20) / 100.0
            scores[token] = round((count / total) * math.log(total + 1.0) * locality_bonus, 6)
        return scores

    def _extract_phrases(self, tokens: list[str]) -> Counter[str]:
        phrases: Counter[str] = Counter()
        limit = min(len(tokens), 160)
        for index in range(limit):
            if index + 1 < limit:
                phrases[f"{tokens[index]} {tokens[index + 1]}"] += 1
            if index + 2 < limit:
                phrases[f"{tokens[index]} {tokens[index + 1]} {tokens[index + 2]}"] += 1
        return phrases

    def _link_tokens(self, tokens: list[str], *, window: int = 3) -> dict[str, dict[str, int]]:
        links: dict[str, dict[str, int]] = defaultdict(dict)
        limit = min(len(tokens), 240)
        for index in range(limit):
            current = tokens[index]
            for offset in range(1, window + 1):
                neighbor_index = index + offset
                if neighbor_index >= limit:
                    break
                neighbor = tokens[neighbor_index]
                links[current][neighbor] = links[current].get(neighbor, 0) + 1
        return {token: dict(sorted(neighbors.items(), key=lambda item: (-item[1], item[0]))[:8]) for token, neighbors in links.items()}

    def _extract_sentences(self, text: str) -> list[str]:
        sentences: list[str] = []
        for part in _SENTENCE_SPLIT_RE.split(text.strip()):
            cleaned = _WHITESPACE_RE.sub(" ", part).strip(" -\t\r\n")
            if cleaned:
                sentences.append(cleaned)
        return sentences[:32]

    def _link_sentences(self, sentences: list[str]) -> list[SentenceEdge]:
        edges: list[SentenceEdge] = []
        for index in range(len(sentences) - 1):
            left = {token for token in _tokenize(sentences[index]) if len(token) > 2}
            right = {token for token in _tokenize(sentences[index + 1]) if len(token) > 2}
            overlap = sorted(left & right)
            if not overlap:
                continue
            weight = round(len(overlap) / max(len(left | right), 1), 3)
            edges.append(SentenceEdge(source=index, target=index + 1, weight=weight, shared_terms=overlap[:10]))
        return edges[:32]

    def _extract_templates(self, text: str) -> list[TemplateRecord]:
        templates: list[TemplateRecord] = []
        for line in text.splitlines():
            stripped = line.strip()
            if not stripped:
                continue
            if ":" in stripped and len(stripped.split(":", 1)[0]) <= 48:
                key, value = stripped.split(":", 1)
                templates.append(TemplateRecord("key_value", {"key": key.strip().lower(), "value": value.strip()}))
                continue
            if "|" in stripped:
                cells = [cell.strip() for cell in stripped.split("|") if cell.strip()]
                if len(cells) >= 2:
                    templates.append(TemplateRecord("table_row", {"cells": cells[:12]}))
                    continue
            if stripped[:1] in {"-", "*"}:
                templates.append(TemplateRecord("bullet", {"value": stripped[1:].strip()}))
        return templates[:32]

    def _build_char_matrix(self, text: str) -> dict[str, list[str]]:
        compact = text.lower().replace(" ", "")
        bigrams = Counter(compact[index:index + 2] for index in range(max(len(compact) - 1, 0)) if compact[index:index + 2].strip())
        trigrams = Counter(compact[index:index + 3] for index in range(max(len(compact) - 2, 0)) if compact[index:index + 3].strip())
        return {
            "bigrams": [chunk for chunk, _ in bigrams.most_common(32)],
            "trigrams": [chunk for chunk, _ in trigrams.most_common(32)],
        }

    def _generate_embedding_keywords(self, text: str) -> list[str]:
        if not self.generator or not hasattr(self.generator, "generate_embedding_keywords"):
            return []
        try:
            generated = self.generator.generate_embedding_keywords(text)
        except Exception:
            return []
        if not isinstance(generated, list):
            return []
        return [str(item).strip().lower() for item in generated if str(item).strip()]

    def _build_prompt_pool(
        self,
        text: str,
        *,
        keywords: list[str],
        phrase_frequency: Counter[str],
        sentence_nodes: list[str],
        templates: list[TemplateRecord],
        knowledge_pool: AnalyticsKnowledgePool | None,
    ) -> dict[str, Any]:
        related: list[dict[str, Any]] = []
        if knowledge_pool is not None:
            for record in knowledge_pool.query(text, top_k=4):
                related.append(asdict(record))
        merged_keywords = list(keywords)
        merged_phrases = list(phrase_frequency.keys())[:24]
        for item in related:
            merged_keywords.extend(item.get("keywords", []))
            merged_phrases.extend(item.get("phrases", []))
        return {
            "keywords": list(dict.fromkeys(merged_keywords))[:64],
            "phrases": list(dict.fromkeys(merged_phrases))[:40],
            "sentences": sentence_nodes[:12],
            "templates": [asdict(item) for item in templates[:12]],
            "related_records": related,
        }

    def _generate_text(
        self,
        text: str,
        *,
        keywords: list[str],
        phrase_frequency: Counter[str],
        sentence_nodes: list[str],
        templates: list[TemplateRecord],
        prompt_pool: dict[str, Any],
    ) -> str:
        prompt = self._build_generation_prompt(
            text,
            keywords=keywords,
            phrase_frequency=phrase_frequency,
            sentence_nodes=sentence_nodes,
            templates=templates,
            prompt_pool=prompt_pool,
        )
        if self.generator and hasattr(self.generator, "generate_text"):
            try:
                generated = self.generator.generate_text(prompt=prompt, context=prompt_pool)
            except Exception:
                generated = ""
            if isinstance(generated, str) and generated.strip():
                return generated.strip()
        return self._fallback_generated_text(
            keywords=keywords,
            phrase_frequency=phrase_frequency,
            sentence_nodes=sentence_nodes,
            templates=templates,
            prompt_pool=prompt_pool,
        )

    def _build_generation_prompt(
        self,
        text: str,
        *,
        keywords: list[str],
        phrase_frequency: Counter[str],
        sentence_nodes: list[str],
        templates: list[TemplateRecord],
        prompt_pool: dict[str, Any],
    ) -> str:
        template_summary = ", ".join(item.template_type for item in templates[:6]) or "none"
        return (
            "Transform the analytics matrices into a compact narrative.\n"
            f"Keywords: {', '.join(keywords[:12])}\n"
            f"Phrases: {', '.join(list(phrase_frequency.keys())[:8])}\n"
            f"Sentences: {' | '.join(sentence_nodes[:4])}\n"
            f"Templates: {template_summary}\n"
            f"Related knowledge count: {len(prompt_pool.get('related_records', []))}\n"
            f"Source text: {text[:1200]}"
        )

    def _fallback_generated_text(
        self,
        *,
        keywords: list[str],
        phrase_frequency: Counter[str],
        sentence_nodes: list[str],
        templates: list[TemplateRecord],
        prompt_pool: dict[str, Any],
    ) -> str:
        parts: list[str] = []
        if keywords:
            parts.append(f"Primary signals: {', '.join(keywords[:8])}.")
        if phrase_frequency:
            parts.append(f"Stable phrases: {'; '.join(list(phrase_frequency.keys())[:5])}.")
        if sentence_nodes:
            parts.append(f"Core narrative: {' '.join(sentence_nodes[:2])}")
        if templates:
            parts.append(f"Structured templates: {', '.join(item.template_type for item in templates[:4])}.")
        related = prompt_pool.get("related_records", [])
        if related:
            parts.append(f"Knowledge pool matched {len(related)} prior records for retrieval reuse.")
        return " ".join(parts).strip()


def _tokenize(text: str) -> list[str]:
    return [token.lower() for token in _TOKEN_RE.findall(text) if len(token.strip()) > 1]
