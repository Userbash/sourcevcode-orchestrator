from __future__ import annotations

from typing import Any


MODEL_WINDOWS = {"local-small": 8000, "gpt-4o": 128000, "gemini-1.5-pro": 1000000}


def resolve_window(model_name: str | None, default: int = 8000) -> int:
    normalized = str(model_name or "").strip().lower()
    if not normalized:
        return default
    return int(MODEL_WINDOWS.get(normalized, default))


def estimate_tokens(text: str) -> int:
    normalized = str(text or "").strip()
    if not normalized:
        return 0
    # Use a conservative estimate so the runtime compresses before cost spikes.
    return max(len(normalized.split()), len(normalized) // 3)


def trim_context_chunks(
    chunks: list[str],
    *,
    window: int,
    keep_ratio: float = 0.45,
) -> dict[str, Any]:
    normalized = [str(chunk).strip() for chunk in chunks if str(chunk).strip()]
    total_tokens = sum(estimate_tokens(chunk) for chunk in normalized)
    threshold = int(window * 0.75)
    if total_tokens <= threshold:
        return {
            "chunks": normalized,
            "dropped_chunks": [],
            "approx_tokens": total_tokens,
            "dropped_count": 0,
            "was_trimmed": False,
        }

    keep_budget = max(1, int(window * keep_ratio))
    kept_reversed: list[str] = []
    kept_tokens = 0
    for chunk in reversed(normalized):
        chunk_tokens = estimate_tokens(chunk)
        if kept_reversed and kept_tokens + chunk_tokens > keep_budget:
            continue
        kept_reversed.append(chunk)
        kept_tokens += chunk_tokens
    kept = list(reversed(kept_reversed))
    dropped = normalized[: max(0, len(normalized) - len(kept))]
    return {
        "chunks": kept,
        "dropped_chunks": dropped,
        "approx_tokens": kept_tokens,
        "dropped_count": len(dropped),
        "was_trimmed": True,
    }
