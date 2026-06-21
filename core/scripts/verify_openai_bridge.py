from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import httpx

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.core.env_loader import load_env_file
from core.core.openai_provider import resolve_openai_provider_config
from core.core.provider_credentials import credential_snapshot

load_env_file(".env")
load_env_file(".env.bridge", override=True)
load_env_file(".env.gemini.local", override=True)


def _auth_headers(api_key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}


def _extract_text(payload: Any) -> str:
    if isinstance(payload, dict):
        choices = payload.get("choices") or []
        if choices:
            message = (choices[0] or {}).get("message") or {}
            text = message.get("content")
            if isinstance(text, str) and text.strip():
                return text.strip()
        output = payload.get("output") or []
        parts: list[str] = []
        for item in output:
            for content in (item.get("content") or []):
                text = content.get("text") or content.get("output_text")
                if isinstance(text, str) and text.strip():
                    parts.append(text.strip())
        if parts:
            return " ".join(parts).strip()
    return ""


def build_summary() -> dict[str, object]:
    credential = credential_snapshot(("OPENAI_API_KEY", "CODEX_SALE_API_KEY"))
    config = resolve_openai_provider_config()
    summary: dict[str, object] = {
        "provider": "openai",
        "configured": bool(credential.get("configured")),
        "usable_by_policy": bool(credential.get("usable")),
        "placeholder": bool(credential.get("placeholder")),
        "base_url": config.base_url,
        "models_endpoint": config.models_endpoint,
        "chat_completions_endpoint": config.chat_completions_endpoint,
        "responses_endpoint": config.responses_endpoint,
        "default_model": config.default_model,
        "ready": False,
    }
    if not credential.get("usable"):
        summary["error"] = "openai_api_key_missing_or_placeholder"
        return summary

    with httpx.Client(timeout=30.0) as client:
        models_probe = {"ok": False, "status_code": None, "model_count": 0, "sample_models": [], "error": None}
        chat_probe = {"ok": False, "status_code": None, "response_sample": "", "error": None}
        responses_probe = {"ok": False, "status_code": None, "response_sample": "", "error": None}
        model_name = config.default_model

        try:
            resp = client.get(config.models_endpoint, headers=_auth_headers(config.api_key))
            models_probe["status_code"] = resp.status_code
            payload = resp.json()
            if resp.status_code < 400:
                items = payload.get("data") or payload.get("models") or []
                models = []
                for item in items:
                    model_id = item.get("id") or item.get("slug") or item.get("name")
                    if isinstance(model_id, str) and model_id.strip():
                        models.append(model_id.strip())
                models_probe["ok"] = True
                models_probe["model_count"] = len(models)
                models_probe["sample_models"] = models[:8]
                if model_name not in models and models:
                    model_name = models[0]
            else:
                models_probe["error"] = resp.text[:400]
        except Exception as exc:
            models_probe["error"] = str(exc)

        try:
            resp = client.post(
                config.chat_completions_endpoint,
                headers=_auth_headers(config.api_key),
                json={"model": model_name, "messages": [{"role": "user", "content": "reply with ok"}], "max_tokens": 8},
            )
            chat_probe["status_code"] = resp.status_code
            payload = resp.json()
            if resp.status_code < 400:
                chat_probe["ok"] = True
                chat_probe["response_sample"] = _extract_text(payload)[:160]
            else:
                chat_probe["error"] = resp.text[:400]
        except Exception as exc:
            chat_probe["error"] = str(exc)

        try:
            resp = client.post(
                config.responses_endpoint,
                headers=_auth_headers(config.api_key),
                json={"model": model_name, "input": "reply with ok", "max_output_tokens": 8},
            )
            responses_probe["status_code"] = resp.status_code
            payload = resp.json()
            if resp.status_code < 400:
                responses_probe["ok"] = True
                responses_probe["response_sample"] = _extract_text(payload)[:160]
            else:
                responses_probe["error"] = resp.text[:400]
        except Exception as exc:
            responses_probe["error"] = str(exc)

    summary["probe_model"] = model_name
    summary["models_probe"] = models_probe
    summary["chat_completions_probe"] = chat_probe
    summary["responses_probe"] = responses_probe
    summary["ready"] = bool(models_probe["ok"] and chat_probe["ok"] and responses_probe["ok"])
    summary["error"] = None if summary["ready"] else chat_probe.get("error") or responses_probe.get("error") or models_probe.get("error")
    return summary


def main() -> None:
    summary = build_summary()
    print(json.dumps(summary, ensure_ascii=True))
    raise SystemExit(0 if summary["ready"] else 1)


if __name__ == "__main__":
    main()
