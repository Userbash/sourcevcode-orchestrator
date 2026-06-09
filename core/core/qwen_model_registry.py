from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass, field
from typing import Any, List, Optional

import requests

logger = logging.getLogger("qwen_model_registry")

@dataclass
class QwenCatalog:
    coder: list[str] = field(default_factory=list)
    instruct: list[str] = field(default_factory=list)
    standard: list[str] = field(default_factory=list)
    plus: list[str] = field(default_factory=list)
    max: list[str] = field(default_factory=list)
    turbo: list[str] = field(default_factory=list)

class QwenModelRegistry:
    """
    Automated discovery and management of Qwen models.
    """
    _cache: QwenCatalog | None = None
    _last_refresh: float = 0
    _refresh_interval: int = 3600 # 1 hour

    def __init__(self) -> None:
        self.api_key = os.getenv("QWEN_API_KEY")
        self.base_url = os.getenv("QWEN_OPENAI_BASE_URL", "https://dashscope.aliyuncs.com/compatible-mode/v1")
        self.local_endpoint = os.getenv("AI_BRIDGE_LOCAL_LLM_ENDPOINT")

    def get_catalog(self, force_refresh: bool = False) -> QwenCatalog:
        now = time.time()
        if self._cache and not force_refresh and (now - self._last_refresh < self._refresh_interval):
            return self._cache

        catalog = QwenCatalog()
        models = self._fetch_models()
        
        for m in models:
            name = m.lower()
            if "coder" in name:
                catalog.coder.append(m)
            elif "instruct" in name:
                catalog.instruct.append(m)
            elif "max" in name:
                catalog.max.append(m)
            elif "plus" in name:
                catalog.plus.append(m)
            elif "turbo" in name:
                catalog.turbo.append(m)
            else:
                catalog.standard.append(m)

        self._cache = catalog
        self._last_refresh = now
        return catalog

    def _fetch_models(self) -> list[str]:
        # 1. Try Local Ollama first if no key
        if not self.api_key and self.local_endpoint:
            try:
                resp = requests.get(f"{self.local_endpoint.rstrip('/')}/api/tags", timeout=5)
                if resp.status_code == 200:
                    data = resp.json()
                    return [m["name"] for m in data.get("models", []) if "qwen" in m["name"].lower()]
            except Exception:
                pass

        # 2. Try Cloud API
        if not self.api_key:
            return ["qwen-2.5-coder-32b", "qwen-2.5-7b-instruct"] # Default fallbacks

        try:
            headers = {"Authorization": f"Bearer {self.api_key}"}
            resp = requests.get(f"{self.base_url.rstrip('/')}/models", headers=headers, timeout=10)
            if resp.status_code == 200:
                data = resp.json()
                return [m["id"] for m in data.get("data", []) if "qwen" in m["id"].lower()]
        except Exception as e:
            logger.error(f"[QWEN_REGISTRY] Failed to fetch cloud models: {e}")
            
        return ["qwen-2.5-coder-32b", "qwen-2.5-7b-instruct"]
