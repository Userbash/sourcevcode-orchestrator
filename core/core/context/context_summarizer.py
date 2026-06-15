from __future__ import annotations


SUMMARY_VERSION = "v1"


def _chunk_summary(chunk: str, *, index: int) -> str:
    words = [word for word in str(chunk).replace("\n", " ").split(" ") if word]
    if not words:
        return f"chunk{index}: empty"
    unique_ratio = len(set(words)) / max(1, len(words))
    if unique_ratio < 0.15:
        return f"chunk{index}: repetitive content x{len(words)}"
    return f"chunk{index}: {' '.join(words[:12])}"


def summarize(chunks: list[str], max_chars: int = 2000) -> str:
    normalized = [str(chunk).strip() for chunk in chunks if str(chunk).strip()]
    if not normalized:
        return f"[summary:{SUMMARY_VERSION} chunks=0]"
    details = " | ".join(_chunk_summary(chunk, index=index + 1) for index, chunk in enumerate(normalized))
    summary = f"[summary:{SUMMARY_VERSION} chunks={len(normalized)}] {details}"
    return summary[:max_chars]
