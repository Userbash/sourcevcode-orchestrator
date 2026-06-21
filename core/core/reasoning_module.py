from __future__ import annotations

import json
import logging
import os
import re
import traceback
from dataclasses import dataclass
from typing import Any, Optional, Type, TypeVar

from pydantic import BaseModel

from .kernel_protocol import KernelAPI
from .openai_provider import build_openai_client_kwargs

T = TypeVar("T", bound=BaseModel)

logger = logging.getLogger("reasoning_module")


def strip_json_fences(raw: str) -> str:
    text = str(raw or "").strip()
    if "```json" in text:
        text = text.split("```json", 1)[1].split("```", 1)[0]
    elif "```" in text:
        text = text.split("```", 1)[1].split("```", 1)[0]
    return text.strip()


def _snake_case_key(value: str) -> str:
    normalized = re.sub(r"(.)([A-Z][a-z]+)", r"\1_\2", value)
    normalized = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", normalized)
    return normalized.replace("-", "_").strip().lower()


def _normalize_payload_keys(payload: Any) -> Any:
    if isinstance(payload, dict):
        return {_snake_case_key(str(key)): _normalize_payload_keys(value) for key, value in payload.items()}
    if isinstance(payload, list):
        return [_normalize_payload_keys(item) for item in payload]
    return payload


@dataclass
class ReasoningModule:
    name: str = "reasoning"
    _api: KernelAPI | None = None
    _client: Any | None = None
    _default_model: str = "gpt-5.5"
    _provider: str = "openai"
    _last_failure: dict[str, Any] | None = None
    _call_stats: dict[str, int] | None = None

    def on_load(self, api: KernelAPI) -> None:
        self._api = api
        self._call_stats = {
            "cloud_success": 0,
            "cloud_failure": 0,
            "local_success": 0,
            "local_failure": 0,
        }

        antigravity_key = (os.getenv("ANTIGRAVITY_API_KEY") or os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY") or "").strip()
        api_key = antigravity_key
        base_url = (
            os.getenv("GEMINI_API_BASE_URL", "https://generativelanguage.googleapis.com/v1beta").rstrip("/") + "/openai/"
            if antigravity_key
            else None
        )
        model = "antigravity-pro"
        provider = "antigravity"

        if not antigravity_key:
            api_key = os.getenv("MISTRAL_API_KEY", "").strip()
            base_url = "https://api.mistral.ai/v1"
            model = "mistral-large-latest"
            provider = "mistral"

        if not api_key:
            api_key = os.getenv("OPENAI_API_KEY", "").strip()
            base_url = build_openai_client_kwargs(max_retries=1).get("base_url")
            model = os.getenv("CODEX_OPENAI_MODEL", "gpt-5.5")
            provider = "openai"

        if api_key:
            try:
                import instructor
                from openai import OpenAI

                kwargs = {"api_key": api_key}
                if base_url:
                    kwargs["base_url"] = base_url
                self._client = instructor.from_openai(OpenAI(**kwargs))
                self._default_model = model
                self._provider = provider
                self._api.log("info", f"[REASONING] Module loaded using {model} (provider={provider}, base_url={base_url})")
            except Exception as e:
                self._api.log("error", f"[REASONING] Failed to initialize instructor client: {e}")

    def on_unload(self) -> None:
        pass

    def _bump_stat(self, key: str) -> None:
        if self._call_stats is None:
            self._call_stats = {}
        self._call_stats[key] = int(self._call_stats.get(key, 0) or 0) + 1

    def _record_failure(
        self,
        *,
        stage: str,
        response_model: Type[T],
        error: Exception,
        prompt: str,
        raw_response: str | None = None,
        requested_model: str | None = None,
    ) -> None:
        self._last_failure = {
            "stage": stage,
            "response_model": response_model.__name__,
            "error_type": type(error).__name__,
            "error": str(error),
            "requested_model": requested_model or "",
            "provider": self._provider,
            "prompt_preview": str(prompt)[:240],
            "raw_response_preview": str(raw_response or "")[:240],
            "traceback": traceback.format_exc(limit=6),
        }

    @staticmethod
    def _validate_response_payload(raw_response: str, response_model: Type[T]) -> T:
        cleaned = strip_json_fences(raw_response)
        try:
            return response_model.model_validate_json(cleaned)
        except Exception:
            payload = json.loads(cleaned)
            return response_model.model_validate(_normalize_payload_keys(payload))

    def diagnostic_snapshot(self) -> dict[str, Any]:
        return {
            "ready": self._client is not None,
            "provider": self._provider,
            "default_model": self._default_model,
            "call_stats": dict(self._call_stats or {}),
            "last_failure": dict(self._last_failure) if isinstance(self._last_failure, dict) else None,
        }

    def _resolve_model(self, requested: Optional[str]) -> str:
        model = (requested or "").strip()
        if not model:
            return self._default_model
        if self._provider == "mistral" and model.startswith("gpt-"):
            return self._default_model
        if self._provider == "antigravity" and model.startswith("gpt-"):
            return self._default_model
        return model

    def structured_call(self, prompt: str, response_model: Type[T], system_prompt: str = "You are a senior system architect.", model: Optional[str] = None) -> Optional[T]:
        if self._client:
            try:
                result = self._client.chat.completions.create(
                    model=self._resolve_model(model),
                    response_model=response_model,
                    messages=[
                        {"role": "system", "content": system_prompt},
                        {"role": "user", "content": prompt},
                    ],
                )
                self._bump_stat("cloud_success")
                self._last_failure = None
                return result
            except Exception as e:
                self._bump_stat("cloud_failure")
                self._record_failure(
                    stage="cloud_structured_call",
                    response_model=response_model,
                    error=e,
                    prompt=prompt,
                    requested_model=model,
                )
                logger.error(f"Cloud structured call failed, trying local LLM fallback: {e}")

        local_llm = self._api.get_module("local_llm") if self._api else None
        if local_llm and getattr(local_llm, "ready", False):
            try:
                sys_prompt = f"{system_prompt}. Return ONLY valid JSON."
                resp = local_llm.query(prompt, system=sys_prompt)
                result = self._validate_response_payload(resp, response_model)
                self._bump_stat("local_success")
                self._last_failure = None
                return result
            except Exception as e:
                self._bump_stat("local_failure")
                self._record_failure(
                    stage="local_llm_fallback",
                    response_model=response_model,
                    error=e,
                    prompt=prompt,
                    raw_response=resp if 'resp' in locals() else None,
                    requested_model=model,
                )
                logger.error(f"Local LLM fallback failed: {e}")

        return None

    def finalize(self) -> dict[str, Any]:
        return self.diagnostic_snapshot()
