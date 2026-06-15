from __future__ import annotations

import re
from typing import Any

from .input_text_normalizer import detect_language_bucket

_INTENT_PATTERNS: tuple[tuple[str, str], ...] = (
    ("plan", r"\b(plan|design|decompose|roadmap|architect|architecture|спланир|разбей|архитект)\b"),
    ("review", r"\b(review|audit|security|scan|ревью|проверь)\b"),
    ("test", r"\b(test|tests|verify|qa|протест|проверка)\b"),
    ("fix", r"\b(fix|bug|repair|patch|исправ|почини)\b"),
    ("docs", r"\b(docs|doc|readme|documentation|док|опиши)\b"),
    ("research", r"\b(research|analyze|analysis|investigate|find|исслед|анализ|найди)\b"),
    ("code", r"\b(build|implement|code|write|develop|backend|frontend|api|ui|реализ|напиш|разработ)\b"),
)


_HIGH_RISK_MARKERS = (
    "production",
    "prod",
    "security",
    "auth",
    "payment",
    "token",
    "secret",
    "migration",
    "database",
    "delete",
    "удали",
)

_MEDIUM_RISK_MARKERS = ("deploy", "release", "refactor", "routing", "policy", "permissions")
_SCOPE_MULTI_AREA_MARKERS = ("backend and frontend", "backend + frontend", "api and ui", "frontend and backend")
_COMPLEXITY_LINKERS = r"\b(and|then|after|plus|also|и|затем|потом)\b"
_MARKER_CANDIDATES = ("backend", "frontend", "api", "ui", "tests", "docs", "security", "migration", "parallel", "review")


def _intent_bucket(text: str, explicit_type: str = "") -> str:
    explicit = str(explicit_type or "").strip().lower()
    if explicit:
        return explicit
    for bucket, pattern in _INTENT_PATTERNS:
        if re.search(pattern, text, flags=re.IGNORECASE):
            return bucket
    return "code"


def _risk_bucket(text: str) -> str:
    lowered = text.lower()
    if any(marker in lowered for marker in _HIGH_RISK_MARKERS):
        return "high"
    if any(marker in lowered for marker in _MEDIUM_RISK_MARKERS):
        return "medium"
    return "low"


def _scope_bucket(text: str, files: list[str]) -> str:
    if len(files) > 1:
        return "multi_file"
    if len(files) == 1:
        return "single_file"
    lowered = text.lower()
    if any(marker in lowered for marker in _SCOPE_MULTI_AREA_MARKERS):
        return "multi_area"
    return "no_file"


def _complexity_bucket(text: str, files: list[str], acceptance: list[str]) -> str:
    score = 0
    score += min(len(files), 4)
    score += min(len(acceptance), 4)
    score += min(len(text) // 160, 4)
    if re.search(_COMPLEXITY_LINKERS, text, flags=re.IGNORECASE):
        score += 1
    if score >= 7:
        return "high"
    if score >= 4:
        return "medium"
    return "low"


def _execution_shape(intent: str, complexity: str, scope: str, text: str) -> str:
    lowered = text.lower()
    if intent == "code" and (complexity == "high" or scope in {"multi_file", "multi_area"}):
        return "parallel_candidate"
    if intent in {"review", "test"}:
        return "single_lane_validation"
    if any(marker in lowered for marker in ("step by step", "пошагово", "decompose", "разбей")):
        return "decomposition_first"
    return "single_lane"


def _input_quality_bucket(text: str) -> str:
    compact = " ".join(text.split()).strip()
    if len(compact) < 12:
        return "sparse"
    if re.search(r"[�]", compact) or compact.count("?") >= 4:
        return "noisy_but_usable"
    return "clean"


def _confidence_score(*, explicit_type: str, files: list[str], acceptance_criteria: list[str], markers: list[str], cleaned_text: str) -> float:
    score = 0.35
    if explicit_type:
        score += 0.20
    if files:
        score += min(0.15, len(files) * 0.04)
    if acceptance_criteria:
        score += min(0.12, len(acceptance_criteria) * 0.04)
    if markers:
        score += min(0.12, len(markers) * 0.03)
    if len(cleaned_text) >= 48:
        score += 0.08
    if len(cleaned_text) >= 160:
        score += 0.05
    return round(min(0.98, score), 2)


def _decision_trust(confidence_score: float) -> str:
    return "trusted" if confidence_score >= 0.72 else "rough_hint"


def quantize_input_text(*, cleaned_text: str, files: list[str] | None = None, acceptance_criteria: list[str] | None = None, explicit_type: str = "") -> dict[str, Any]:
    files = list(files or [])
    acceptance_criteria = list(acceptance_criteria or [])
    language = detect_language_bucket(cleaned_text)
    intent = _intent_bucket(cleaned_text, explicit_type=explicit_type)
    complexity = _complexity_bucket(cleaned_text, files, acceptance_criteria)
    risk = _risk_bucket(cleaned_text)
    scope = _scope_bucket(cleaned_text, files)
    execution = _execution_shape(intent, complexity, scope, cleaned_text)
    quality = _input_quality_bucket(cleaned_text)
    markers: list[str] = []
    matched_rules: list[str] = []
    reasons: list[str] = []
    lowered = cleaned_text.lower()
    for marker in _MARKER_CANDIDATES:
        if marker in lowered:
            markers.append(marker)
            matched_rules.append(f"marker:{marker}")
    if explicit_type:
        matched_rules.append(f"explicit_type:{explicit_type}")
        reasons.append(f"Task type was explicitly provided as {explicit_type}.")
    if files:
        matched_rules.append(f"files:{len(files)}")
        reasons.append(f"Task references {len(files)} file(s), which improves routing confidence.")
    if acceptance_criteria:
        matched_rules.append(f"acceptance:{len(acceptance_criteria)}")
        reasons.append(f"Task includes {len(acceptance_criteria)} acceptance criteria.")
    if risk != "low":
        matched_rules.append(f"risk:{risk}")
    if execution != "single_lane":
        matched_rules.append(f"execution:{execution}")
    reasons.append(f"Intent classified as {intent} for execution planning.")
    reasons.append(f"Complexity classified as {complexity} with scope={scope} and execution={execution}.")
    reasons.append(f"Input quality classified as {quality} in language bucket {language}.")
    confidence_score = _confidence_score(
        explicit_type=explicit_type,
        files=files,
        acceptance_criteria=acceptance_criteria,
        markers=markers,
        cleaned_text=cleaned_text,
    )
    return {
        "cleaned_text": cleaned_text,
        "language_bucket": language,
        "intent_bucket": intent,
        "complexity_bucket": complexity,
        "risk_bucket": risk,
        "scope_bucket": scope,
        "execution_shape": execution,
        "input_quality_bucket": quality,
        "markers": markers[:8],
        "matched_rules": matched_rules[:12],
        "reasons": reasons[:8],
        "confidence_score": confidence_score,
        "decision_mode": "heuristic",
        "decision_trust": _decision_trust(confidence_score),
    }
