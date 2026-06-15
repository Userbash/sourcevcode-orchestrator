from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Any, Optional, Type, TypeVar

from pydantic import BaseModel

from .kernel_protocol import KernelAPI

T = TypeVar("T", bound=BaseModel)

logger = logging.getLogger("reasoning_module")


def strip_json_fences(raw: str) -> str:
    text = str(raw or "").strip()
    if "```json" in text:
        text = text.split("```json", 1)[1].split("```", 1)[0]
    elif "```" in text:
        text = text.split("```", 1)[1].split("```", 1)[0]
    return text.strip()


@dataclass
class ReasoningModule:
    name: str = "reasoning"
    _api: KernelAPI | None = None
    _client: Any | None = None
    _default_model: str = "gpt-4o"
    _provider: str = "openai"

    def on_load(self, api: KernelAPI) -> None:
        self._api = api

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
            base_url = None
            model = "gpt-4o"
            provider = "openai"

        if api_key:
            try:
                import instructor
                from openai import OpenAI

                self._client = instructor.from_openai(OpenAI(api_key=api_key, base_url=base_url))
                self._default_model = model
                self._provider = provider
                self._api.log("info", f"[REASONING] Module loaded using {model} (provider={provider}, base_url={base_url})")
            except Exception as e:
                self._api.log("error", f"[REASONING] Failed to initialize instructor client: {e}")

    def on_unload(self) -> None:
        pass

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
                return self._client.chat.completions.create(
                    model=self._resolve_model(model),
                    response_model=response_model,
                    messages=[
                        {"role": "system", "content": system_prompt},
                        {"role": "user", "content": prompt},
                    ],
                )
            except Exception as e:
                logger.error(f"Cloud structured call failed, trying local LLM fallback: {e}")

        local_llm = self._api.get_module("local_llm") if self._api else None
        if local_llm and getattr(local_llm, "ready", False):
            try:
                sys_prompt = f"{system_prompt}. Return ONLY valid JSON."
                resp = local_llm.query(prompt, system=sys_prompt)
                return response_model.model_validate_json(strip_json_fences(resp))
            except Exception as e:
                logger.error(f"Local LLM fallback failed: {e}")

        return None

    def finalize(self) -> dict[str, Any]:
        return {"ready": self._client is not None}
