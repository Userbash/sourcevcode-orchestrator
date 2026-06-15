from .context_window import MODEL_WINDOWS
from .context_policy import should_compress
from .context_window import estimate_tokens, resolve_window, trim_context_chunks
from .context_summarizer import summarize


class NativeContextManager:
    def build_context(self, task, memory_refs=None):
        refs = memory_refs or []
        model_name = getattr(task, "assigned_model", None) if task is not None else None
        window = resolve_window(model_name, MODEL_WINDOWS.get("local-small", 8000))
        text = "\n".join(str(ref).strip() for ref in refs if str(ref).strip())
        token_count = estimate_tokens(text)
        summary = ""
        summary_version = ""
        dropped_count = 0
        was_compressed = False
        if should_compress(token_count, window):
            trimmed = trim_context_chunks(list(refs), window=window)
            dropped_count = int(trimmed["dropped_count"])
            was_compressed = bool(trimmed["was_trimmed"])
            if dropped_count:
                summary = summarize(trimmed["dropped_chunks"])
                summary_version = "v1"
            body = "\n".join(trimmed["chunks"])
            text = "\n".join(part for part in (summary, body) if part)
            token_count = estimate_tokens(text)
        return {
            "context": text,
            "window": window,
            "summary": summary,
            "summary_version": summary_version,
            "dropped_count": dropped_count,
            "was_compressed": was_compressed,
            "approx_tokens": token_count,
        }
