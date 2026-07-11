from __future__ import annotations

import asyncio
import concurrent.futures
import json
import os
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from core.core.antigravity_model_registry import AntigravityModelRegistry
from core.core.mistral_model_registry import MistralModelRegistry
from core.core.model_inventory_index import ModelInventoryIndex
from core.core.model_health_registry import ModelHealthRegistry
from core.core.local_llm_bridge import LocalLLMBridge
from core.core.local_model_runtime import LocalModelRuntime, LocalModelRuntimeConfig
from core.core.openai_model_registry import OpenAIModelRegistry
from core.core.openai_compatible_inventory import is_text_compatible_model, sync_openai_compatible_artifacts
from core.core.openai_bazzite_endpoint import load_openai_endpoint_discovery
from core.core.openai_provider import openai_endpoint_manifest, resolve_openai_provider_config, resolve_openai_provider_identity
from core.core.antigravity_provider import fetch_antigravity_model_catalog, resolve_antigravity_provider_config
from core.core.ai_kernel_bridge import AIKernelBridge
from core.core.model_usage_module import ModelUsageModule
from core.core.mimo_provider import configured_native_mimo_models, extract_mimo_response_text, fetch_mimo_model_catalog, invoke_mimo_group_probe, mimo_group_description, mimo_group_use_case, mimo_model_group, mimo_model_subgroup, mimo_probe_mode_for_group, sync_mimo_native_artifacts
from core.core.mimo_status import mimo_enabled
import httpx
import requests

_OPENAI_RUNTIME_BLOCK_MARKERS = (
    "unsupported model",
    "model is not supported",
    "invalid model",
    "does not exist",
    "not found",
    "no eligible resources",
)

_OPENAI_RUNTIME_MODEL_UNAVAILABLE_MARKERS = (
    "model is not available",
    "model unavailable",
    "not available for",
    "is not available",
    "is unavailable",
)


@dataclass(slots=True)
class ProviderInventoryEntry:
    provider: str
    fetched_at: int
    ok: bool
    source: str
    models: list[str] = field(default_factory=list)
    error: str | None = None
    status_code: int | None = None
    diagnostics: dict[str, Any] = field(default_factory=dict)


class ProviderInventoryService:
    @staticmethod
    def _testing_mode() -> bool:
        return os.getenv("TESTING", "").strip().lower() == "true" or bool(os.getenv("PYTEST_CURRENT_TEST"))

    @staticmethod
    def _read_int_env(name: str, default: int) -> int:
        raw = str(os.getenv(name, str(default)) or str(default)).strip()
        try:
            return int(raw)
        except ValueError:
            return default

    def __init__(self) -> None:
        self.snapshot_path = Path(os.getenv("PROVIDER_INVENTORY_SNAPSHOT_PATH", "core/.cache/provider_inventory_snapshot.json"))
        self.openai = OpenAIModelRegistry()
        self.antigravity = AntigravityModelRegistry()
        self.mistral = MistralModelRegistry()
        self.mimo_enabled = mimo_enabled()
        self.mimo_auto_ping_enabled = self.mimo_enabled and os.getenv("AI_BRIDGE_MIMO_AUTO_PING_ENABLED", "true").strip().lower() in {"1", "true", "yes", "on"}
        try:
            self.mimo_auto_ping_interval_sec = max(60, int(os.getenv("AI_BRIDGE_MIMO_AUTO_PING_INTERVAL_SEC", "1800") or "1800"))
        except ValueError:
            self.mimo_auto_ping_interval_sec = 1800
        self._last_mimo_auto_ping_at = 0.0
        try:
            self.snapshot_refresh_interval_sec = max(60, int(os.getenv("AI_BRIDGE_PROVIDER_INVENTORY_REFRESH_INTERVAL_SEC", "1800") or "1800"))
        except ValueError:
            self.snapshot_refresh_interval_sec = 1800
        self.openai_runtime_inventory_path = Path(os.getenv("OPENAI_RUNTIME_INVENTORY_PATH", "core/.cache/openai_runtime_inventory.json"))
        self.model_index_path = Path(os.getenv("PROVIDER_MODEL_INDEX_PATH", "core/.cache/provider_model_index.json"))
        self.model_index = ModelInventoryIndex(self.model_index_path)
        self.model_health = ModelHealthRegistry()
        self.ai_kernel_bridge = AIKernelBridge()
        self._entry_refresh_intervals_sec = {
            "openai": self._read_int_env("AI_BRIDGE_PROVIDER_REFRESH_OPENAI_SEC", 300),
            "mistral": self._read_int_env("AI_BRIDGE_PROVIDER_REFRESH_MISTRAL_SEC", 300),
            "antigravity": self._read_int_env("AI_BRIDGE_PROVIDER_REFRESH_ANTIGRAVITY_SEC", 120),
            "mimo": self._read_int_env("AI_BRIDGE_PROVIDER_REFRESH_MIMO_SEC", 300),
            "local_llm": self._read_int_env("AI_BRIDGE_PROVIDER_REFRESH_LOCAL_LLM_SEC", 30),
            "ai_kernel": self._read_int_env("AI_BRIDGE_PROVIDER_REFRESH_AI_KERNEL_SEC", 30),
        }
        self.model_index.load()

    @staticmethod
    def _normalize_provider(provider: str) -> str:
        raw = str(provider or "").strip().lower()
        if raw in {"google", "antigravity", "gemini"}:
            return "antigravity"
        if raw in {"local_llm", "ollama", "local"}:
            return "local_llm"
        if raw in {"ai-kernel", "ai_kernel", "llama_cpp", "llama-cpp"}:
            return "ai_kernel"
        if raw in {"mimo", "mimo-cli", "xiaomi", "github-copilot", "github-models"}:
            return "mimo"
        return raw

    @staticmethod
    def _report_dir() -> Path:
        explicit = os.getenv("PROVIDER_INVENTORY_REPORT_DIR", "").strip()
        if explicit:
            return Path(explicit)
        workspace = Path("/workspace/reports/model_ping")
        if workspace.parent.exists():
            return workspace
        return Path.cwd() / "reports" / "model_ping"

    @staticmethod
    def _load_json(path: Path) -> dict[str, Any]:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            return payload if isinstance(payload, dict) else {}
        except Exception:
            return {}

    def _ping_artifacts(self) -> dict[str, dict[str, Any]]:
        report_dir = self._report_dir()
        return {
            "model_ping": self._load_json(report_dir / "model_ping_report.json"),
            "mimo_ping": self._load_json(report_dir / "mimo_model_ping_report.json"),
            "mimo_usable": self._load_json(report_dir / "mimo_usable_models.json"),
            "failed_by_provider": self._load_json(report_dir / "failed_models_by_provider.json"),
        }

    @staticmethod
    def _generated_mimo_models() -> list[str]:
        path = Path(os.getenv("MIMO_MODELS_FULL_CACHE_PATH", "core/.cache/mimo_models_full.json"))
        cache_models: list[str] = []
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            rows = payload.get("models") if isinstance(payload, dict) else []
            if isinstance(rows, list):
                cache_models.extend(str(item).strip() for item in rows if str(item).strip())
        except Exception:
            pass

        models = cache_models if any("mimo-" in str(model).lower() for model in cache_models) else []
        generated_root = Path(os.getenv("MIMO_GENERATED_PROFILE_DIR", "core/mimo/profiles/generated/mimo_native"))
        manifest_path = generated_root / "manifest.json"
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except Exception:
            manifest = {}

        if not models:
            manifest_models = manifest.get("models") if isinstance(manifest, dict) else []
            if isinstance(manifest_models, list):
                models.extend(str(item).strip() for item in manifest_models if str(item).strip())

        if not models:
            model_profiles = manifest.get("model_profiles") if isinstance(manifest, dict) else []
            for rel_path in model_profiles if isinstance(model_profiles, list) else []:
                profile_path = generated_root / str(rel_path)
                try:
                    profile = json.loads(profile_path.read_text(encoding="utf-8"))
                except Exception:
                    continue
                profile_key = str(profile.get("profile_key") or "").strip()
                if not profile_key.startswith("model::"):
                    continue
                metadata = profile.get("metadata") if isinstance(profile.get("metadata"), dict) else {}
                family = str(metadata.get("model_family") or "").strip().lower()
                model_name = profile_key.split("model::", 1)[1].strip()
                if model_name and (family == "mimo" or "mimo-" in model_name.lower()):
                    models.append(model_name)

        seen: set[str] = set()
        generated: list[str] = []
        for model in models:
            model_name = str(model).strip()
            lowered = model_name.lower()
            normalized = model_name.split("/", 1)[1] if "/" in model_name else model_name
            if not normalized.lower().startswith("mimo-") or normalized in seen:
                continue
            seen.add(normalized)
            generated.append(normalized)
        return generated

    @staticmethod
    def _openai_artifact_models() -> list[str]:
        paths = [
            Path(os.getenv("OPENAI_MODELS_FULL_CACHE_PATH", "core/.cache/openai_models_full.json")),
            Path(os.getenv("OPENAI_GENERATED_PROFILE_DIR", "core/mimo/profiles/generated/openai_compatible")) / "manifest.json",
            Path(os.getenv("OPENAI_MODEL_TEMPLATE_MANIFEST_PATH", "core/mimo/profiles/generated/openai_compatible/model_template_manifest.json")),
            Path(os.getenv("OPENAI_ORCHESTRATOR_TEMPLATES_PATH", "core/mimo/profiles/generated/openai_compatible/orchestrator_templates.json")),
        ]
        models: list[str] = []
        for path in paths:
            payload = ProviderInventoryService._load_json(path)
            if not payload:
                continue
            rows = payload.get("models")
            if isinstance(rows, list):
                for item in rows:
                    if isinstance(item, dict):
                        model_name = str(item.get("model_name") or item.get("model") or item.get("id") or "").strip()
                    else:
                        model_name = str(item).strip()
                    if model_name and is_text_compatible_model(model_name):
                        models.append(model_name)
            role_map = payload.get("roles")
            if isinstance(role_map, dict):
                for entries in role_map.values():
                    if not isinstance(entries, list):
                        continue
                    for row in entries:
                        if not isinstance(row, dict):
                            continue
                        model_name = str(row.get("model_name") or "").strip()
                        if model_name and is_text_compatible_model(model_name):
                            models.append(model_name)
        return ProviderInventoryService._merge_model_names(models)

    @classmethod
    def _openai_configured_models(cls) -> list[str]:
        configured: list[str] = []
        for env_name in (
            "CODEX_OPENAI_MODEL",
            "OPENAI_DEFAULT_MODEL",
            "OPENAI_HIGH_MODELS",
            "OPENAI_MEDIUM_MODELS",
            "OPENAI_LOW_MODELS",
            "OPENAI_EXTRA_MODELS",
            "OPENAI_CRITICAL_MODELS",
        ):
            raw = str(os.getenv(env_name, "") or "").strip()
            if not raw:
                continue
            items = [item.strip() for item in raw.split(",")] if "," in raw else [raw]
            for item in items:
                if item and is_text_compatible_model(item):
                    configured.append(item)
        return cls._merge_model_names(configured)

    @classmethod
    def _inventory_source_paths(cls) -> list[Path]:
        report_dir = cls._report_dir()
        paths = [
            Path(os.getenv("MIMO_MODELS_FULL_CACHE_PATH", "core/.cache/mimo_models_full.json")),
            Path(os.getenv("MIMO_GENERATED_PROFILE_DIR", "core/mimo/profiles/generated/mimo_native")) / "manifest.json",
            Path(os.getenv("OPENAI_MODELS_FULL_CACHE_PATH", "core/.cache/openai_models_full.json")),
            Path(os.getenv("OPENAI_GENERATED_PROFILE_DIR", "core/mimo/profiles/generated/openai_compatible")) / "manifest.json",
            report_dir / "model_ping_report.json",
            report_dir / "mimo_model_ping_report.json",
            report_dir / "mimo_usable_models.json",
            report_dir / "failed_models_by_provider.json",
        ]
        return [path for path in paths if path.exists()]

    @classmethod
    def _latest_inventory_source_mtime(cls) -> int | None:
        mtimes: list[int] = []
        for path in cls._inventory_source_paths():
            try:
                mtimes.append(int(path.stat().st_mtime))
            except Exception:
                continue
        return max(mtimes) if mtimes else None

    @staticmethod
    def _merge_model_names(*groups: list[str]) -> list[str]:
        merged: list[str] = []
        seen: set[str] = set()
        for group in groups:
            for item in group:
                model_name = str(item or "").strip()
                if not model_name or model_name in seen:
                    continue
                seen.add(model_name)
                merged.append(model_name)
        return merged

    @classmethod
    def _openai_probe_limit(cls) -> int:
        return cls._read_int_env("AI_BRIDGE_OPENAI_RUNTIME_PROBE_LIMIT", 8)

    @staticmethod
    def _openai_probe_prompt() -> str:
        return str(os.getenv("AI_BRIDGE_OPENAI_RUNTIME_PROBE_PROMPT", "reply with ok") or "reply with ok").strip() or "reply with ok"

    @staticmethod
    def _select_openai_probe_models(models: list[str], *, default_model: str = "", limit: int = 8) -> list[str]:
        preferred: list[str] = []
        if default_model and is_text_compatible_model(default_model):
            preferred.append(default_model)
        for env_name in ("CODEX_OPENAI_MODEL", "OPENAI_HIGH_MODELS", "OPENAI_MEDIUM_MODELS", "OPENAI_LOW_MODELS", "OPENAI_EXTRA_MODELS"):
            raw = str(os.getenv(env_name, "") or "").strip()
            if not raw:
                continue
            preferred.extend(item.strip() for item in raw.split(",") if item.strip() and is_text_compatible_model(item.strip()))

        ordered = ProviderInventoryService._merge_model_names(preferred, [model for model in models if is_text_compatible_model(model)])
        if limit <= 0:
            return ordered
        return ordered[:limit]

    def _read_openai_runtime_inventory_cache(self) -> dict[str, Any]:
        if not self.openai_runtime_inventory_path.exists():
            return {}
        payload = self._load_json(self.openai_runtime_inventory_path)
        return payload if isinstance(payload, dict) else {}

    @staticmethod
    def _validated_row_lookup(rows: list[dict[str, Any]] | None) -> dict[str, dict[str, Any]]:
        lookup: dict[str, dict[str, Any]] = {}
        for row in rows or []:
            if not isinstance(row, dict):
                continue
            model_name = str(row.get("model") or "").strip()
            if model_name:
                lookup[model_name] = dict(row)
        return lookup

    def _merge_openai_validated_rows(
        self,
        *,
        previous_rows: list[dict[str, Any]] | None,
        current_rows: list[dict[str, Any]] | None,
        models: list[str],
    ) -> list[dict[str, Any]]:
        previous_lookup = self._validated_row_lookup(previous_rows)
        current_lookup = self._validated_row_lookup(current_rows)
        merged: list[dict[str, Any]] = []
        for model_name in models:
            row = current_lookup.get(model_name) or previous_lookup.get(model_name)
            if row:
                merged.append(dict(row))
        return merged

    def _select_openai_probe_models_with_rotation(
        self,
        models: list[str],
        *,
        default_model: str = "",
        limit: int = 8,
        previous_runtime: dict[str, Any] | None = None,
    ) -> list[str]:
        ordered = self._select_openai_probe_models(models, default_model=default_model, limit=0)
        if limit <= 0:
            return ordered

        previous_runtime = previous_runtime if isinstance(previous_runtime, dict) else {}
        previous_selected = [
            str(model).strip()
            for model in (previous_runtime.get("selected_models") or [])
            if str(model).strip()
        ]
        previous_validated = [
            str(row.get("model") or "").strip()
            for row in (previous_runtime.get("validated_models") or [])
            if isinstance(row, dict) and str(row.get("model") or "").strip()
        ]
        previously_seen = set(previous_selected + previous_validated)
        rotating_tail = [model for model in ordered if model not in previously_seen]
        rotating_tail.extend(model for model in ordered if model in previously_seen)
        return rotating_tail[:limit]

    @staticmethod
    def _extract_openai_probe_text(payload: Any) -> str:
        if isinstance(payload, dict):
            choices = payload.get("choices") or []
            if isinstance(choices, list) and choices:
                message = (choices[0] or {}).get("message") or {}
                content = message.get("content")
                if isinstance(content, str) and content.strip():
                    return content.strip()[:160]
            output = payload.get("output") or []
            if isinstance(output, list):
                parts: list[str] = []
                for item in output:
                    if not isinstance(item, dict):
                        continue
                    for content in item.get("content") or []:
                        if not isinstance(content, dict):
                            continue
                        text_part = content.get("text") or content.get("output_text")
                        if isinstance(text_part, str) and text_part.strip():
                            parts.append(text_part.strip())
                if parts:
                    return " ".join(parts)[:160]
        return ""

    @staticmethod
    def _extract_openai_probe_error(payload: Any, fallback: str = "") -> str:
        if isinstance(payload, dict):
            error = payload.get("error")
            if isinstance(error, dict):
                for key in ("message", "detail", "error", "type"):
                    value = str(error.get(key) or "").strip()
                    if value:
                        return value[:240]
            if isinstance(error, str) and error.strip():
                return error.strip()[:240]
            for key in ("message", "detail"):
                value = str(payload.get(key) or "").strip()
                if value:
                    return value[:240]
        return str(fallback or "").strip()[:240]

    @staticmethod
    def _openai_status_family(status_code: int | None) -> str | None:
        if status_code is None:
            return None
        if 100 <= status_code < 200:
            return "1xx"
        if 200 <= status_code < 300:
            return "2xx"
        if 300 <= status_code < 400:
            return "3xx"
        if 400 <= status_code < 500:
            return "4xx"
        if 500 <= status_code < 600:
            return "5xx"
        return None

    @classmethod
    def _classify_openai_probe_failure(
        cls,
        *,
        endpoint_name: str,
        status_code: int | None,
        error: str,
    ) -> dict[str, Any]:
        lowered = str(error or "").strip().lower()
        retryable = False
        model_blocked = False

        if "no eligible resources" in lowered:
            kind = "messages_pool_unavailable" if endpoint_name.startswith("messages") else "provider_pool_unavailable"
            retryable = True
        elif any(marker in lowered for marker in _OPENAI_RUNTIME_BLOCK_MARKERS):
            kind = "unsupported_model"
            model_blocked = True
        elif any(marker in lowered for marker in _OPENAI_RUNTIME_MODEL_UNAVAILABLE_MARKERS):
            kind = "model_unavailable"
            model_blocked = True
        elif status_code == 400:
            kind = "invalid_request"
        elif status_code == 401 or any(marker in lowered for marker in ("invalid api key", "unauthorized", "authentication required", "auth failed")):
            kind = "auth_failed"
        elif status_code == 403:
            kind = "forbidden"
        elif status_code == 404:
            kind = "not_found"
        elif status_code == 408:
            kind = "request_timeout"
            retryable = True
        elif status_code == 409:
            kind = "conflict"
        elif status_code == 429:
            kind = "rate_limited"
            retryable = True
        elif status_code == 501:
            kind = "not_implemented"
        elif status_code in {502, 503, 504}:
            kind = "upstream_unavailable"
            retryable = True
        elif status_code is not None and status_code >= 500:
            kind = "upstream_error"
            retryable = True
        elif status_code is None:
            kind = "network_error"
            retryable = True
        else:
            kind = "probe_failed"

        return {
            "error_kind": kind,
            "retryable": retryable,
            "model_blocked": model_blocked,
            "status_family": cls._openai_status_family(status_code),
        }

    @classmethod
    def _openai_endpoint_probe_names(cls) -> tuple[str, ...]:
        return ("chat_completions", "responses", "messages", "messages_count_tokens")

    @classmethod
    def _collect_openai_endpoint_failures(cls, row: dict[str, Any]) -> list[dict[str, Any]]:
        failures: list[dict[str, Any]] = []
        for endpoint_name in cls._openai_endpoint_probe_names():
            payload = row.get(endpoint_name)
            if not isinstance(payload, dict) or payload.get("ok") or payload.get("skipped"):
                continue
            failures.append({
                "endpoint": endpoint_name,
                "endpoint_url": str(payload.get("endpoint_url") or ""),
                "status_code": payload.get("status_code"),
                "status_family": payload.get("status_family"),
                "error_kind": str(payload.get("error_kind") or "probe_failed"),
                "error": str(payload.get("error") or ""),
                "retryable": bool(payload.get("retryable")),
                "model_blocked": bool(payload.get("model_blocked")),
            })
        return failures

    @classmethod
    def _build_openai_endpoint_probe_summary(
        cls,
        rows: list[dict[str, Any]],
        endpoint_manifest: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        manifest_endpoints = (endpoint_manifest or {}).get("endpoints") if isinstance(endpoint_manifest, dict) else {}
        summary: dict[str, Any] = {}
        for endpoint_name in cls._openai_endpoint_probe_names():
            endpoint_url = str((manifest_endpoints or {}).get(endpoint_name) or "")
            bucket = {
                "configured": bool(endpoint_url),
                "endpoint_url": endpoint_url,
                "ok_count": 0,
                "failed_count": 0,
                "skipped_count": 0,
                "status_code_counts": {},
                "error_kind_counts": {},
                "retryable_count": 0,
                "model_blocked_count": 0,
                "failed_models": [],
            }
            for row in rows:
                payload = row.get(endpoint_name)
                if not isinstance(payload, dict):
                    continue
                if payload.get("skipped"):
                    bucket["skipped_count"] += 1
                    continue
                if payload.get("ok"):
                    bucket["ok_count"] += 1
                    continue
                bucket["failed_count"] += 1
                status_code = payload.get("status_code")
                if status_code is not None:
                    key = str(status_code)
                    bucket["status_code_counts"][key] = int(bucket["status_code_counts"].get(key) or 0) + 1
                error_kind = str(payload.get("error_kind") or "probe_failed")
                bucket["error_kind_counts"][error_kind] = int(bucket["error_kind_counts"].get(error_kind) or 0) + 1
                if payload.get("retryable"):
                    bucket["retryable_count"] += 1
                if payload.get("model_blocked"):
                    bucket["model_blocked_count"] += 1
                bucket["failed_models"].append({
                    "model": str(row.get("model") or ""),
                    "status_code": status_code,
                    "error_kind": error_kind,
                })
            summary[endpoint_name] = bucket
        return summary

    async def _probe_openai_runtime_matrix_async(
        self,
        *,
        model_names: list[str],
        api_key: str,
        chat_endpoint: str,
        responses_endpoint: str,
        messages_endpoint: str,
        messages_count_tokens_endpoint: str,
        prompt: str,
        max_parallel: int,
    ) -> list[dict[str, Any]]:
        timeout = httpx.Timeout(30.0)
        semaphore = asyncio.Semaphore(max(1, max_parallel))
        headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}

        async with httpx.AsyncClient(timeout=timeout) as client:
            async def _call(
                endpoint_name: str,
                endpoint_url: str,
                payload: dict[str, Any],
                *,
                extra_headers: dict[str, str] | None = None,
            ) -> dict[str, Any]:
                if not str(endpoint_url or "").strip():
                    return {
                        "ok": False,
                        "status_code": None,
                        "error": "endpoint_not_configured",
                        "response_sample": "",
                        "endpoint": endpoint_name,
                        "endpoint_url": "",
                        "skipped": True,
                        "error_kind": "endpoint_not_configured",
                        "retryable": False,
                        "model_blocked": False,
                        "status_family": None,
                    }
                async with semaphore:
                    try:
                        request_headers = dict(headers)
                        if extra_headers:
                            request_headers.update(extra_headers)
                        response = await client.post(endpoint_url, headers=request_headers, json=payload)
                    except Exception as exc:
                        failure = self._classify_openai_probe_failure(endpoint_name=endpoint_name, status_code=None, error=str(exc))
                        return {
                            "ok": False,
                            "status_code": None,
                            "error": str(exc)[:240],
                            "response_sample": "",
                            "endpoint": endpoint_name,
                            "endpoint_url": endpoint_url,
                            "skipped": False,
                            **failure,
                        }
                    try:
                        body: Any = response.json() if response.content else {}
                    except Exception:
                        body = {}
                    if response.status_code < 400:
                        return {
                            "ok": True,
                            "status_code": int(response.status_code),
                            "error": None,
                            "response_sample": self._extract_openai_probe_text(body),
                            "endpoint": endpoint_name,
                            "endpoint_url": endpoint_url,
                            "skipped": False,
                            "error_kind": None,
                            "retryable": False,
                            "model_blocked": False,
                            "status_family": self._openai_status_family(int(response.status_code)),
                        }
                    error_text = self._extract_openai_probe_error(body, response.text)
                    failure = self._classify_openai_probe_failure(
                        endpoint_name=endpoint_name,
                        status_code=int(response.status_code),
                        error=error_text,
                    )
                    return {
                        "ok": False,
                        "status_code": int(response.status_code),
                        "error": error_text,
                        "response_sample": "",
                        "endpoint": endpoint_name,
                        "endpoint_url": endpoint_url,
                        "skipped": False,
                        **failure,
                    }

            async def _probe_model(model_name: str) -> dict[str, Any]:
                chat_payload = {"model": model_name, "messages": [{"role": "user", "content": prompt}], "max_tokens": 8}
                responses_payload = {"model": model_name, "input": prompt, "max_output_tokens": 8}
                messages_payload = {"model": model_name, "messages": [{"role": "user", "content": prompt}], "max_tokens": 8}
                count_tokens_payload = {"model": model_name, "messages": [{"role": "user", "content": prompt}]}
                anthropic_headers = {"anthropic-version": "2023-06-01"}
                results = await asyncio.gather(
                    _call("chat_completions", chat_endpoint, chat_payload),
                    _call("responses", responses_endpoint, responses_payload),
                    _call("messages", messages_endpoint, messages_payload, extra_headers=anthropic_headers),
                    _call("messages_count_tokens", messages_count_tokens_endpoint, count_tokens_payload, extra_headers=anthropic_headers),
                )
                row = {
                    "model": model_name,
                    "chat_completions": results[0],
                    "responses": results[1],
                    "messages": results[2],
                    "messages_count_tokens": results[3],
                    "fully_routable": bool(results[0].get("ok") and results[1].get("ok")),
                }
                row["endpoint_failures"] = self._collect_openai_endpoint_failures(row)
                return row

            return await asyncio.gather(*(_probe_model(model_name) for model_name in model_names))

    @staticmethod
    def _openai_probe_availability(row: dict[str, Any]) -> dict[str, Any]:
        chat = row.get("chat_completions") if isinstance(row.get("chat_completions"), dict) else {}
        responses = row.get("responses") if isinstance(row.get("responses"), dict) else {}
        messages = row.get("messages") if isinstance(row.get("messages"), dict) else {}
        messages_count_tokens = row.get("messages_count_tokens") if isinstance(row.get("messages_count_tokens"), dict) else {}
        chat_ok = bool(chat.get("ok"))
        responses_ok = bool(responses.get("ok"))
        messages_ok = bool(messages.get("ok"))
        messages_count_tokens_ok = bool(messages_count_tokens.get("ok"))
        chat_status = int(chat.get("status_code") or 0) if str(chat.get("status_code") or '').strip() else None
        responses_status = int(responses.get("status_code") or 0) if str(responses.get("status_code") or '').strip() else None
        errors = ' '.join(filter(None, [
            str(chat.get("error") or '').strip().lower(),
            str(responses.get("error") or '').strip().lower(),
            str(messages.get("error") or '').strip().lower(),
            str(messages_count_tokens.get("error") or '').strip().lower(),
        ]))
        endpoint_failures = row.get("endpoint_failures") if isinstance(row.get("endpoint_failures"), list) else []
        text_present = bool(str(chat.get("response_sample") or '').strip() or str(responses.get("response_sample") or '').strip())
        criteria = {
            "chat_completions_ok": chat_ok,
            "responses_ok": responses_ok,
            "messages_ok": messages_ok,
            "messages_count_tokens_ok": messages_count_tokens_ok,
            "http_chat_success": chat_status is not None and chat_status < 400,
            "http_responses_success": responses_status is not None and responses_status < 400,
            "text_response_present": text_present,
            "blocked_marker_found": any(marker in errors for marker in _OPENAI_RUNTIME_BLOCK_MARKERS),
            "auth_failed": any(code in {401, 403} for code in (chat_status, responses_status) if code is not None),
            "rate_limited": any(code == 429 for code in (chat_status, responses_status) if code is not None),
            "server_error": any(code >= 500 for code in (chat_status, responses_status) if code is not None),
            "messages_pool_unavailable": any(str(item.get("error_kind") or "") == "messages_pool_unavailable" for item in endpoint_failures if isinstance(item, dict)),
            "retryable_endpoint_failure": any(bool(item.get("retryable")) for item in endpoint_failures if isinstance(item, dict)),
        }
        if chat_ok and responses_ok:
            availability = "available"
            reason = "chat_and_responses_ready"
        elif criteria["auth_failed"]:
            availability = "auth_failed"
            reason = "auth_status"
        elif criteria["rate_limited"]:
            availability = "rate_limited"
            reason = "quota_or_rate_limit"
        elif criteria["server_error"]:
            availability = "upstream_error"
            reason = "provider_http_5xx"
        elif criteria["blocked_marker_found"]:
            availability = "blocked"
            reason = "runtime_block_marker"
        elif chat_ok or responses_ok:
            availability = "partial"
            reason = "single_endpoint_ready"
        else:
            availability = "unavailable"
            reason = "probe_failed"
        return {
            "available": availability == "available",
            "availability": availability,
            "reason": reason,
            "criteria": criteria,
            "claude_messages_ready": messages_ok,
            "claude_count_tokens_ready": messages_count_tokens_ok,
        }

    @staticmethod
    def _openai_model_cost_snapshot(model_name: str) -> dict[str, Any]:
        usage = ModelUsageModule()
        input_only = usage.estimate_usage_cost(model_name, input_tokens=1000, output_tokens=0, provider="openai")
        output_only = usage.estimate_usage_cost(model_name, input_tokens=0, output_tokens=1000, provider="openai")
        balanced = usage.estimate_usage_cost(model_name, input_tokens=1000, output_tokens=1000, provider="openai")
        return {
            "currency": "USD",
            "input_usd_per_1k": round(float(input_only.get("estimated_cost_usd") or 0.0), 6),
            "output_usd_per_1k": round(float(output_only.get("estimated_cost_usd") or 0.0), 6),
            "blended_usd_per_2k": round(float(balanced.get("estimated_cost_usd") or 0.0), 6),
        }

    @classmethod
    def _build_openai_runtime_recommendations(cls, payload: dict[str, Any]) -> dict[str, Any]:
        templates = (payload.get("model_templates") or {}).get("models") if isinstance(payload.get("model_templates"), dict) else []
        rows = templates if isinstance(templates, list) else []
        enriched: list[dict[str, Any]] = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            model_name = str(row.get("model_name") or '').strip()
            if not model_name:
                continue
            cost = cls._openai_model_cost_snapshot(model_name)
            role_scores = row.get("role_scores") if isinstance(row.get("role_scores"), dict) else {}
            enriched.append({
                "model_name": model_name,
                "status": str(row.get("status") or "discovered"),
                "role_scores": role_scores,
                "preferred_task_types": list(row.get("preferred_task_types") or []),
                "cost": cost,
            })
        routable = [row for row in enriched if row.get("status") == "routable"]
        candidates = routable or enriched
        def _sorted_for(role: str) -> list[dict[str, Any]]:
            return sorted(
                candidates,
                key=lambda row: (-float((row.get("role_scores") or {}).get(role, 0.0)), float((row.get("cost") or {}).get("blended_usd_per_2k", 0.0)), row.get("model_name") or ""),
            )
        recommendations = {role: [row.get("model_name") for row in _sorted_for(role)[:5]] for role in ("code_parallel", "review_primary", "plan_primary", "test_primary", "docs_primary", "research_primary")}
        economy = sorted(candidates, key=lambda row: (float((row.get("cost") or {}).get("blended_usd_per_2k", 0.0)), -float((row.get("role_scores") or {}).get("docs_primary", 0.0)), row.get("model_name") or ""))
        premium = sorted(candidates, key=lambda row: (-max(float(score) for score in (row.get("role_scores") or {"_": 0.0}).values()), float((row.get("cost") or {}).get("blended_usd_per_2k", 0.0)), row.get("model_name") or ""))
        defaults = {
            "best_overall": [row.get("model_name") for row in premium[:5]],
            "economy": [row.get("model_name") for row in economy[:5]],
            "premium": [row.get("model_name") for row in premium[:5]],
            "cheapest_routable": economy[0].get("model_name") if economy else None,
            "strongest_routable": premium[0].get("model_name") if premium else None,
        }
        return {
            "roles": recommendations,
            "defaults": defaults,
            "selection_policy": {
                "availability_required": True,
                "prefer_routable_models": True,
                "sort_order": ["role_score_desc", "cost_asc", "model_name_asc"],
            },
        }

    def refresh_openai_runtime_inventory(self, *, force_refresh: bool = False, probe_limit: int | None = None) -> dict[str, Any]:
        live_models = self.openai.get_models(force_refresh=force_refresh)
        diagnostics = dict(self.openai.diagnostics())
        discovery = load_openai_endpoint_discovery()
        cfg = resolve_openai_provider_config()
        previous_runtime = self._read_openai_runtime_inventory_cache()
        diagnostics["discovery"] = discovery
        sync_summary: dict[str, Any] = {}
        artifact_models = self._openai_artifact_models()
        configured_models = self._openai_configured_models()
        models = self._merge_model_names(live_models, artifact_models, configured_models)
        diagnostics["artifact_model_count"] = len(artifact_models)
        diagnostics["configured_model_count"] = len(configured_models)
        diagnostics["effective_model_count"] = len(models)

        selected = self._select_openai_probe_models_with_rotation(
            models,
            default_model=str(getattr(cfg, "default_model", "") or ""),
            limit=self._openai_probe_limit() if probe_limit is None else int(probe_limit),
            previous_runtime=previous_runtime,
        )
        probe_rows: list[dict[str, Any]] = []
        execution_mode = "skipped"
        if selected and str(getattr(cfg, "api_key", "") or "").strip():
            runner = lambda: asyncio.run(
                self._probe_openai_runtime_matrix_async(
                    model_names=selected,
                    api_key=str(cfg.api_key),
                    chat_endpoint=str(cfg.chat_completions_endpoint),
                    responses_endpoint=str(cfg.responses_endpoint),
                    messages_endpoint=str(getattr(cfg, "messages_endpoint", "") or ""),
                    messages_count_tokens_endpoint=str(getattr(cfg, "messages_count_tokens_endpoint", "") or ""),
                    prompt=self._openai_probe_prompt(),
                    max_parallel=max(2, min(16, len(selected) * 4)),
                )
            )
            try:
                asyncio.get_running_loop()
            except RuntimeError:
                execution_mode = "asyncio_run"
                probe_rows = runner()
            else:
                execution_mode = "threadpool_asyncio_run"
                with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
                    probe_rows = pool.submit(runner).result()

        enriched_probe_rows: list[dict[str, Any]] = []
        for row in probe_rows:
            availability = self._openai_probe_availability(row)
            enriched = dict(row)
            enriched.update(availability)
            enriched["cost"] = self._openai_model_cost_snapshot(str(row.get("model") or ""))
            enriched_probe_rows.append(enriched)

        merged_validated_rows = self._merge_openai_validated_rows(
            previous_rows=previous_runtime.get("validated_models") if isinstance(previous_runtime, dict) else None,
            current_rows=enriched_probe_rows,
            models=models,
        )

        if models:
            try:
                sync_summary = sync_openai_compatible_artifacts(
                    models,
                    base_url=str(cfg.base_url or "").strip(),
                    validated_rows=merged_validated_rows,
                    default_model=str(getattr(cfg, "default_model", "") or ""),
                )
            except Exception as exc:
                sync_summary = {"ok": False, "error": str(exc)}

        fully_routable = [row for row in merged_validated_rows if bool(row.get("available"))]
        chat_ready = [row for row in merged_validated_rows if bool(((row.get("chat_completions") or {}).get("ok")))]
        responses_ready = [row for row in merged_validated_rows if bool(((row.get("responses") or {}).get("ok")))]
        messages_ready = [row for row in merged_validated_rows if bool(((row.get("messages") or {}).get("ok")))]
        messages_count_tokens_ready = [row for row in merged_validated_rows if bool(((row.get("messages_count_tokens") or {}).get("ok")))]
        identity = resolve_openai_provider_identity(cfg)
        endpoint_manifest = openai_endpoint_manifest(cfg)
        endpoint_probe_summary = self._build_openai_endpoint_probe_summary(merged_validated_rows, endpoint_manifest)
        payload = {
            "provider": "openai",
            "provider_id": identity["provider_id"],
            "provider_name": identity["provider_name"],
            "fetched_at": int(time.time()),
            "force_refresh": bool(force_refresh),
            "registry_diagnostics": diagnostics,
            "models": models,
            "model_count": len(models),
            "model_preview": models[:50],
            "selected_models": selected,
            "selected_model_count": len(selected),
            "validated_models": merged_validated_rows,
            "validated_model_count": len(merged_validated_rows),
            "fully_routable_models": [str(row.get("model") or "") for row in fully_routable],
            "fully_routable_count": len(fully_routable),
            "chat_ready_models": [str(row.get("model") or "") for row in chat_ready],
            "chat_ready_count": len(chat_ready),
            "responses_ready_models": [str(row.get("model") or "") for row in responses_ready],
            "responses_ready_count": len(responses_ready),
            "messages_ready_models": [str(row.get("model") or "") for row in messages_ready],
            "messages_ready_count": len(messages_ready),
            "messages_count_tokens_ready_models": [str(row.get("model") or "") for row in messages_count_tokens_ready],
            "messages_count_tokens_ready_count": len(messages_count_tokens_ready),
            "endpoint_probe_summary": endpoint_probe_summary,
            "artifact_sync": sync_summary,
            "model_templates": (sync_summary or {}).get("model_template_manifest", {}) if isinstance(sync_summary, dict) else {},
            "base_url": str(getattr(cfg, "base_url", "") or ""),
            "models_endpoint": str(getattr(cfg, "models_endpoint", "") or ""),
            "chat_completions_endpoint": str(getattr(cfg, "chat_completions_endpoint", "") or ""),
            "responses_endpoint": str(getattr(cfg, "responses_endpoint", "") or ""),
            "messages_endpoint": str(getattr(cfg, "messages_endpoint", "") or ""),
            "messages_count_tokens_endpoint": str(getattr(cfg, "messages_count_tokens_endpoint", "") or ""),
            "codex_endpoint": str(getattr(cfg, "codex_endpoint", "") or ""),
            "endpoint_manifest": endpoint_manifest,
            "default_model": str(getattr(cfg, "default_model", "") or ""),
            "pricing": {model: self._openai_model_cost_snapshot(model) for model in models[:100]},
            "execution_mode": execution_mode,
        }
        payload["recommended_models"] = self._build_openai_runtime_recommendations(payload)
        self.openai_runtime_inventory_path.parent.mkdir(parents=True, exist_ok=True)
        self.openai_runtime_inventory_path.write_text(json.dumps(payload, ensure_ascii=True, indent=2), encoding="utf-8")
        return payload

    def _artifact_mimo_models(self) -> dict[str, list[str]]:
        artifacts = self._ping_artifacts()
        usable_rows = artifacts.get("mimo_usable", {}).get("models", [])
        ping_rows = artifacts.get("mimo_ping", {}).get("models", [])
        usable_models: list[str] = []
        ping_models: list[str] = []

        for row in usable_rows if isinstance(usable_rows, list) else []:
            if not isinstance(row, dict):
                continue
            model_name = str(row.get("model") or "").strip()
            if model_name:
                usable_models.append(model_name)

        for row in ping_rows if isinstance(ping_rows, list) else []:
            if not isinstance(row, dict):
                continue
            model_name = str(row.get("model") or "").strip()
            if model_name:
                ping_models.append(model_name)

        return {
            "usable": self._merge_model_names(usable_models),
            "ping": self._merge_model_names(ping_models),
        }

    def _read_snapshot_file(self) -> dict[str, Any]:
        if not self.snapshot_path.exists():
            return {"updated_at": None, "providers": {}, "model_health": self.model_health.load()}
        try:
            payload = json.loads(self.snapshot_path.read_text(encoding="utf-8"))
            if isinstance(payload, dict):
                providers = payload.get("providers")
                if isinstance(providers, dict):
                    if not isinstance(payload.get("model_health"), dict):
                        payload["model_health"] = self.model_health.load()
                    return payload
        except Exception:
            pass
        return {"updated_at": None, "providers": {}, "model_health": self.model_health.load()}

    def _snapshot_is_stale(self, payload: dict[str, Any]) -> bool:
        updated_at = payload.get("updated_at") if isinstance(payload, dict) else None
        if not isinstance(updated_at, int):
            return True
        latest_source_mtime = self._latest_inventory_source_mtime()
        if latest_source_mtime is not None and latest_source_mtime > updated_at:
            return True
        return (time.time() - updated_at) >= self.snapshot_refresh_interval_sec

    @staticmethod
    def _parse_mimo_text_events(output: str) -> str:
        parts: list[str] = []
        for line in str(output or '').splitlines():
            stripped = line.strip()
            if not stripped:
                continue
            try:
                event = json.loads(stripped)
            except json.JSONDecodeError:
                continue
            if isinstance(event, dict) and event.get('type') == 'text':
                part = event.get('part') or {}
                text_part = str(part.get('text') or '').strip()
                if text_part:
                    parts.append(text_part)
        return ' '.join(parts).strip()

    @staticmethod
    def _select_mimo_probe_models(models: list[str]) -> list[str]:
        try:
            raw_limit = str(os.getenv('AI_BRIDGE_MIMO_AUTO_PING_LIMIT', '0') or '0').strip()
            limit = int(raw_limit)
        except ValueError:
            limit = 0
        unique: list[str] = []
        seen: set[str] = set()
        def add(candidate: str) -> None:
            if candidate and candidate not in seen:
                unique.append(candidate)
                seen.add(candidate)
        preferred_prefixes = ('xiaomi/mimo-v2.5-pro', 'xiaomi/mimo-v2.5', 'xiaomi/mimo-v2-pro', 'xiaomi/mimo-v2-omni', 'xiaomi/mimo-v2-flash')
        for prefix in preferred_prefixes:
            for model in models:
                if model.startswith(prefix):
                    add(model)
        for model in models:
            if model.startswith('xiaomi/'):
                add(model)
        provider_seen: set[str] = set()
        for model in models:
            provider = model.split('/', 1)[0] if '/' in model else 'mimo'
            if provider in provider_seen:
                continue
            provider_seen.add(provider)
            add(model)
        for model in models:
            add(model)
            if limit > 0 and len(unique) >= limit:
                break
        return unique[:limit] if limit > 0 else unique

    def refresh_mimo_usable_snapshot(self, *, force_refresh: bool = False, prompt: str = 'reply with pong only') -> dict[str, Any]:
        if not self.mimo_enabled:
            return {'status': 'disabled', 'reason': 'mimo_disabled_by_env', 'disable_env': 'AI_BRIDGE_MIMO_ENABLED', 'groups': []}
        report_dir = self._report_dir()
        report_dir.mkdir(parents=True, exist_ok=True)
        now = time.time()
        if not force_refresh and self._last_mimo_auto_ping_at and now - self._last_mimo_auto_ping_at < self.mimo_auto_ping_interval_sec:
            return {'status': 'skipped', 'reason': 'interval_not_elapsed', 'next_refresh_in_sec': max(0, int(self.mimo_auto_ping_interval_sec - (now - self._last_mimo_auto_ping_at)))}
        inventory = configured_native_mimo_models()
        selected = self._select_mimo_probe_models(inventory)
        rows: list[dict[str, Any]] = []
        ready_rows: list[dict[str, Any]] = []
        text_ready_rows: list[dict[str, Any]] = []
        specialized_ready_rows: list[dict[str, Any]] = []
        group_buckets: dict[str, dict[str, Any]] = {}

        def bucket_for(group: str) -> dict[str, Any]:
            bucket = group_buckets.setdefault(
                group,
                {
                    'group': group,
                    'description': mimo_group_description(group),
                    'use_case': mimo_group_use_case(group),
                    'probe_mode': mimo_probe_mode_for_group(group),
                    'inventory_count': 0,
                    'probed_count': 0,
                    'ready_count': 0,
                    'failed_count': 0,
                    'models': [],
                    'ready_models': [],
                    'failed_models': [],
                },
            )
            return bucket

        for model_name in selected:
            group = mimo_model_group(model_name)
            probe_mode = mimo_probe_mode_for_group(group)
            row: dict[str, Any] = {
                'model': model_name,
                'group': group,
                'group_description': mimo_group_description(group),
                'group_use_case': mimo_group_use_case(group),
                'subgroup': mimo_model_subgroup(model_name),
                'probe_mode': probe_mode,
            }
            payload, error, status_code, probe_group = invoke_mimo_group_probe(model_name, prompt + ' and no reasoning', group=group, timeout_sec=20.0)
            text_output = extract_mimo_response_text(payload) if payload else ''
            ready = False
            if group in {'text', 'multimodal'}:
                ready = bool(text_output)
                if text_output:
                    row['response_sample'] = text_output[:120]
                    row['response_kind'] = 'text'
                elif status_code is not None and status_code < 400:
                    row['response_kind'] = 'non_text'
            elif status_code is not None and status_code < 400:
                ready = True
                row['response_kind'] = 'specialized_ok'
                if text_output:
                    row['response_sample'] = text_output[:120]
            row['status_code'] = status_code
            row['probe_group'] = probe_group
            if ready:
                row['ok'] = True
                ready_rows.append({
                    'model': model_name,
                    'group': group,
                    'ok': True,
                    'response_sample': row.get('response_sample'),
                    'status_code': status_code,
                    'probe_mode': probe_mode,
                })
                if group in {'text', 'multimodal'}:
                    text_ready_rows.append(dict(ready_rows[-1]))
                else:
                    specialized_ready_rows.append(dict(ready_rows[-1]))
                bucket = bucket_for(group)
                bucket['ready_count'] += 1
                bucket['ready_models'].append(model_name)
            else:
                row['ok'] = False
                row['error'] = str(error or 'no_text_events')[:240]
                bucket = bucket_for(group)
                bucket['failed_count'] += 1
                bucket['failed_models'].append({'model': model_name, 'error': row['error'], 'status_code': status_code})
            bucket['inventory_count'] += 1
            bucket['probed_count'] += 1
            bucket['models'].append(model_name)
            rows.append(row)

        group_rows = [group_buckets[group] for group in sorted(group_buckets, key=lambda item: {'text': 0, 'multimodal': 1, 'asr': 2, 'tts': 3}.get(item, 99))]
        report_payload = {
            'provider': 'mimo',
            'generated_at': int(now),
            'inventory_count': len(inventory),
            'probed_count': len(selected),
            'ok': len(ready_rows),
            'failed': len(rows) - len(ready_rows),
            'models': rows,
            'groups': group_rows,
            'group_ready_counts': {item['group']: item['ready_count'] for item in group_rows},
            'group_failed_counts': {item['group']: item['failed_count'] for item in group_rows},
        }
        usable_payload = {
            'provider': 'mimo',
            'generated_at': int(now),
            'usable_count': len(ready_rows),
            'text_ready_count': len(text_ready_rows),
            'specialized_ready_count': len(specialized_ready_rows),
            'total': len(selected),
            'models': ready_rows,
            'groups': group_rows,
        }
        (report_dir / 'mimo_model_ping_report.json').write_text(json.dumps(report_payload, ensure_ascii=True, indent=2), encoding='utf-8')
        (report_dir / 'mimo_usable_models.json').write_text(json.dumps(usable_payload, ensure_ascii=True, indent=2), encoding='utf-8')
        self._last_mimo_auto_ping_at = now
        return {
            'status': 'ok',
            'inventory_count': len(inventory),
            'probed_count': len(selected),
            'usable_count': len(ready_rows),
            'text_ready_count': len(text_ready_rows),
            'specialized_ready_count': len(specialized_ready_rows),
            'groups': group_rows,
            'report_dir': str(report_dir),
        }

    def _openai_entry(self, *, force_refresh: bool = False) -> ProviderInventoryEntry:
        runtime_inventory: dict[str, Any] | None = None
        if force_refresh:
            runtime_inventory = self.refresh_openai_runtime_inventory(force_refresh=True)
            diag = runtime_inventory.get("registry_diagnostics") if isinstance(runtime_inventory, dict) else {}
            models = list(runtime_inventory.get("models") or []) if isinstance(runtime_inventory, dict) else []
            diagnostics = dict(diag) if isinstance(diag, dict) else {}
            diagnostics["artifact_sync"] = (runtime_inventory or {}).get("artifact_sync", {}) if isinstance(runtime_inventory, dict) else {}
            diagnostics["runtime_inventory"] = runtime_inventory or {}
            return ProviderInventoryEntry(
                provider="openai",
                fetched_at=int(time.time()),
                ok=bool(models),
                source=str(diagnostics.get("source") or "live"),
                models=models,
                error=str(diagnostics.get("error_message") or "") or None,
                diagnostics=diagnostics,
            )

        models = self.openai.get_models(force_refresh=False)
        diag = self.openai.diagnostics()
        sync_summary: dict[str, Any] = {}
        if models:
            try:
                cfg = resolve_openai_provider_config()
                sync_summary = sync_openai_compatible_artifacts(models, base_url=str(cfg.base_url or '').strip())
            except Exception as exc:
                sync_summary = {"ok": False, "error": str(exc)}
        diagnostics = dict(diag)
        diagnostics["artifact_sync"] = sync_summary
        diagnostics["discovery"] = load_openai_endpoint_discovery()
        return ProviderInventoryEntry(
            provider="openai",
            fetched_at=int(time.time()),
            ok=bool(models),
            source=str(diag.get("source") or "cache"),
            models=models,
            error=str(diag.get("error_message") or "") or None,
            diagnostics=diagnostics,
        )

    def _antigravity_entry(self, *, force_refresh: bool = False) -> ProviderInventoryEntry:
        catalog = fetch_antigravity_model_catalog(force_refresh=force_refresh, timeout_sec=float(getattr(self.antigravity, "timeout", 20.0) or 20.0))
        cfg = resolve_antigravity_provider_config()
        models = [str(model).strip() for model in list(catalog.get("models") or []) if str(model).strip()]
        default_model = str(cfg.default_model or "").strip()
        diagnostics = {
            "cache_path": str(self.antigravity.cache_path),
            "ttl_sec": self.antigravity.ttl_sec,
            "base_url": cfg.base_url,
            "models_endpoint": cfg.models_endpoint,
            "default_model": default_model,
            "model_alias_present": default_model in models if default_model else False,
            "catalog": catalog,
        }
        error = str(catalog.get("error") or "") or (None if models else "inventory_unavailable")
        return ProviderInventoryEntry(
            provider="antigravity",
            fetched_at=int(time.time()),
            ok=bool(catalog.get("ok")) and bool(models),
            source=str(catalog.get("source") or "registry"),
            models=models,
            error=error,
            status_code=int(catalog["status_code"]) if catalog.get("status_code") is not None else None,
            diagnostics=diagnostics,
        )

    def _mistral_entry(self, *, force_refresh: bool = False) -> ProviderInventoryEntry:
        models = self.mistral.get_models(force_refresh=force_refresh)
        diag = self.mistral.diagnostics()
        return ProviderInventoryEntry(
            provider="mistral",
            fetched_at=int(time.time()),
            ok=bool(models),
            source=str(diag.get("source") or "live"),
            models=models,
            error=str(diag.get("error_message") or "") or None,
            status_code=int(diag["status_code"]) if diag.get("status_code") is not None else None,
            diagnostics=diag,
        )

    def _local_llm_entry(self, *, force_refresh: bool = False) -> ProviderInventoryEntry:
        config = LocalModelRuntimeConfig.from_env()
        runtime = LocalModelRuntime(config)
        runtime_config = getattr(runtime, "config", config)
        configured_model = str(getattr(runtime_config, "default_model", "") or getattr(runtime_config, "model_name", "") or "").strip()
        candidate_endpoints = list(getattr(runtime_config, "endpoints", ()) or ())
        configured_endpoint = str(getattr(runtime_config, "endpoint", "") or getattr(runtime, "current_endpoint", "") or "")
        gpu_backend = str(os.getenv("AI_BRIDGE_LOCAL_LLM_GPU_BACKEND", "") or os.getenv("AI_BRIDGE_LOCAL_LLM_GPU_BACKEND_DETECTED", "") or "").strip()
        gpu_enabled = (os.getenv("OLLAMA_GPU_ENABLED", "1").strip().lower() not in {"0", "false", "no", "off"})
        default_options = dict(getattr(runtime_config, "default_options", {}) or {})
        bridge = LocalLLMBridge()
        diagnostics: dict[str, Any] = {
            "configured_endpoint": configured_endpoint,
            "candidate_endpoints": candidate_endpoints,
            "default_model": configured_model,
            "gpu": {
                "backend": gpu_backend or "auto",
                "enabled": gpu_enabled,
                "forced": bool(default_options.get("num_gpu")),
                "num_gpu_layers": int(default_options.get("num_gpu") or 0),
                "main_gpu": default_options.get("main_gpu"),
            },
        }
        if force_refresh:
            bridge.ensure_ready(configured_model or None)
            if bridge.last_bootstrap_status:
                diagnostics["bootstrap"] = dict(bridge.last_bootstrap_status)
        try:
            health = runtime.check_health_sync(configured_model or None)
            residents = runtime.list_resident_models_sync()
            resident_models = [str(item.name).strip() for item in residents if str(getattr(item, "name", "") or "").strip()]
            resident_details = [
                {
                    "name": str(getattr(item, "name", "") or "").strip(),
                    "size": getattr(item, "size", None),
                    "size_vram": getattr(item, "size_vram", None),
                    "expires_at": getattr(item, "expires_at", None),
                    "digest": getattr(item, "digest", None),
                }
                for item in residents
                if str(getattr(item, "name", "") or "").strip()
            ]
            gpu_active = any(int(row.get("size_vram") or 0) > 0 for row in resident_details)
            diagnostics.update({
                "runtime_status": str(getattr(health, "status", "unknown") or "unknown"),
                "active_endpoint": str((getattr(health, "endpoint", None) or getattr(runtime, "current_endpoint", configured_endpoint) or configured_endpoint)),
                "latency_ms": float(getattr(health, "latency_ms", 0.0) or 0.0),
                "attempts": int(getattr(health, "attempts", 0) or 0),
                "model_present": bool(getattr(health, "model_present", False)),
                "resident_models": resident_models,
                "resident_details": resident_details,
                "resident_model_count": len(resident_models),
                "gpu": {
                    **dict(diagnostics.get("gpu") or {}),
                    "active": gpu_active,
                    "resident_vram_bytes": sum(max(0, int(row.get("size_vram") or 0)) for row in resident_details),
                },
            })
            models = [str(item).strip() for item in list(getattr(health, "available_models", []) or []) if str(item).strip()]
            return ProviderInventoryEntry(
                provider="local_llm",
                fetched_at=int(time.time()),
                ok=bool(getattr(health, "ok", False)),
                source="ollama_http",
                models=models,
                error=str(getattr(health, "error", "") or "") or None,
                status_code=int(getattr(health, "status_code", 0) or 0) or None,
                diagnostics=diagnostics,
            )
        except Exception as exc:
            diagnostics.update({
                "runtime_status": "offline",
                "active_endpoint": getattr(runtime, "current_endpoint", configured_endpoint),
                "resident_models": [],
                "resident_model_count": 0,
            })
            if bridge.last_bootstrap_status and "bootstrap" not in diagnostics:
                diagnostics["bootstrap"] = dict(bridge.last_bootstrap_status)
            return ProviderInventoryEntry(
                provider="local_llm",
                fetched_at=int(time.time()),
                ok=False,
                source="ollama_http",
                models=[],
                error=str(exc),
                diagnostics=diagnostics,
            )

    def _ai_kernel_entry(self, *, force_refresh: bool = False) -> ProviderInventoryEntry:
        if os.getenv("AI_KERNEL_ENABLED", "true").strip().lower() not in {"1", "true", "yes", "on"}:
            return ProviderInventoryEntry(
                provider='ai_kernel',
                fetched_at=int(time.time()),
                ok=False,
                source='disabled_by_env',
                models=[],
                error='ai_kernel_disabled_by_env',
                diagnostics={'enabled': False, 'disable_env': 'AI_KERNEL_ENABLED', 'inventory_status': 'offline'},
            )
        alias = (os.getenv("AI_KERNEL_MODEL_ALIAS") or "hauhaucs-qwen36-35b-a3b-aggressive:q4_k_m").strip()
        gate = self.ai_kernel_bridge.gate(model_name=alias, ensure_ready=force_refresh)
        models = [str(item).strip() for item in (gate.get('models') or []) if str(item).strip()]
        probe = gate.get('probe') if isinstance(gate.get('probe'), dict) else {}
        status_code = probe.get('status_code') if isinstance(probe, dict) else None
        error = probe.get('error') if isinstance(probe, dict) else None
        if not models and alias and not error:
            error = 'models_unavailable'
        alias_present = bool(gate.get('model_alias_present'))
        inventory_status = 'ready' if alias_present else ('degraded' if models else 'offline')
        diagnostics = {
            'base_url': gate.get('base_url'),
            'model_alias': alias,
            'model_alias_present': alias_present,
            'inventory_status': inventory_status,
            'ready': bool(gate.get('ready')),
            'reachable': bool(gate.get('reachable')),
            'attempted_autostart': bool(gate.get('attempted_autostart')),
            'service_process_active': bool(gate.get('service_process_active')),
            'autostart_enabled': bool(gate.get('autostart_enabled')),
            'manage_remote_enabled': bool(gate.get('manage_remote_enabled')),
            'probe': probe,
        }
        return ProviderInventoryEntry(
            provider='ai_kernel',
            fetched_at=int(time.time()),
            ok=bool(models),
            source='openai_compatible',
            models=models,
            error=str(error) if error else None,
            status_code=int(status_code) if isinstance(status_code, int) else None,
            diagnostics=diagnostics,
        )

    def _mimo_entry(self, *, force_refresh: bool = False) -> ProviderInventoryEntry:
        if not self.mimo_enabled:
            return ProviderInventoryEntry(provider="mimo", fetched_at=int(time.time()), ok=False, source="disabled_by_env", models=[], error="mimo_disabled_by_env", diagnostics={"enabled": False, "disable_env": "AI_BRIDGE_MIMO_ENABLED"})
        live_catalog = fetch_mimo_model_catalog(force_refresh=force_refresh)
        sync_summary: dict[str, Any] = {}
        if force_refresh or live_catalog.get("source") == "live":
            try:
                sync_summary = sync_mimo_native_artifacts(list(live_catalog.get("models") or []), force_refresh=False)
            except Exception as exc:
                sync_summary = {"ok": False, "error": str(exc)}
        artifact_models = self._artifact_mimo_models()
        usable_models = [model for model in artifact_models["usable"] if str(model).startswith('xiaomi/')]
        ping_models = [model for model in artifact_models["ping"] if str(model).startswith('xiaomi/')]
        generated_models = [f'xiaomi/{name}' if '/' not in str(name) else str(name) for name in self._generated_mimo_models()]
        live_models = [str(model).strip() for model in list(live_catalog.get("models") or []) if str(model).strip()]
        configured_models = configured_native_mimo_models() if not live_models else []
        merged = self._merge_model_names(live_models, configured_models, generated_models, usable_models, ping_models)
        if live_models:
            source = "direct_http_catalog"
        elif generated_models:
            source = "generated_manifest"
        elif usable_models or ping_models:
            source = "artifact_reports"
        else:
            source = str(live_catalog.get("source") or "direct_http_catalog")
        status_code = int(live_catalog["status_code"]) if live_catalog.get("status_code") is not None else None
        live_error = str(live_catalog.get("error") or "").strip()
        billing_blocked = status_code == 402 or any(marker in live_error.lower() for marker in ("payment required", "insufficient account balance", "billing hard limit", "credit balance"))
        error = live_error or "native_model_catalog_empty"
        if merged and not billing_blocked:
            error = None
        return ProviderInventoryEntry(
            provider="mimo",
            fetched_at=int(time.time()),
            ok=bool(merged) and not billing_blocked,
            source=source,
            models=merged,
            error=error,
            status_code=status_code,
            diagnostics={
                "direct_http_only": True,
                "live_catalog": live_catalog,
                "artifact_sync": sync_summary,
                "generated_models_count": len(generated_models),
                "usable_artifact_models_count": len(usable_models),
                "ping_artifact_models_count": len(ping_models),
                "runtime_status": "billing_blocked" if billing_blocked else ("ready" if merged else "offline"),
            },
        )


    @staticmethod
    def _classify_provider_error(entry: dict[str, Any]) -> str:
        diagnostics = entry.get("diagnostics") if isinstance(entry, dict) else {}
        diagnostics = diagnostics if isinstance(diagnostics, dict) else {}
        bootstrap = diagnostics.get("bootstrap") if isinstance(diagnostics.get("bootstrap"), dict) else {}
        status_code = entry.get("status_code")
        bootstrap_state = str(bootstrap.get("state") or "").strip().lower()
        bootstrap_reason = str(bootstrap.get("reason") or bootstrap.get("error") or "").strip().lower()
        error_text = str(entry.get("error") or diagnostics.get("error_message") or diagnostics.get("error_type") or "").strip().lower()
        if bootstrap_state == "runtime_missing" or any(marker in bootstrap_reason for marker in ("ollama_not_installed", "ollama_install_failed", "command not found")):
            return "runtime_missing"
        if status_code in {401, 403} or "auth" in error_text or "invalid api key" in error_text or "missing_api_key" in error_text:
            return "auth_failed"
        if status_code == 402 or any(marker in error_text for marker in ("payment required", "insufficient account balance", "billing hard limit", "credit balance")):
            return "billing_blocked"
        if status_code == 429 or "rate limit" in error_text or "quota" in error_text:
            return "quota_exceeded"
        if any(marker in error_text for marker in ("ollama_not_installed", "ollama_install_failed", "command not found")):
            return "runtime_missing"
        if any(marker in error_text for marker in ("connection refused", "timeout", "temporarily unavailable", "endpoint_unavailable", "tcp_timeout")):
            return "offline"
        if error_text:
            return "degraded"
        return "ready" if bool(entry.get("ok")) else "unknown"

    @staticmethod
    def _usage_rows(usage_snapshot: dict[str, Any] | None) -> tuple[dict[tuple[str, str], dict[str, Any]], dict[str, dict[str, Any]]]:
        snapshot = usage_snapshot if isinstance(usage_snapshot, dict) else {}
        history = snapshot.get("history") if isinstance(snapshot.get("history"), list) else []
        stats = (snapshot.get("stats") or {}).get("models") if isinstance(snapshot.get("stats"), dict) else {}
        by_model: dict[tuple[str, str], dict[str, Any]] = {}
        by_provider: dict[str, dict[str, Any]] = {}
        for row in history:
            if not isinstance(row, dict):
                continue
            provider = ProviderInventoryService._normalize_provider(str(row.get("provider") or "unknown").strip().lower())
            model = str(row.get("model") or "unknown").strip()
            key = (provider, model)
            item = by_model.setdefault(key, {
                "provider": provider,
                "model_name": model,
                "tokens_used": 0,
                "estimated_cost_usd": 0.0,
                "requests_count": 0,
            })
            item["tokens_used"] += max(0, int(row.get("tokens_used") or 0))
            item["estimated_cost_usd"] = round(float(item["estimated_cost_usd"]) + float(row.get("estimated_cost_usd") or 0.0), 6)
            item["requests_count"] += 1
        for (provider, model), item in by_model.items():
            stat_row = stats.get(model) if isinstance(stats, dict) else None
            if isinstance(stat_row, dict):
                item.update({
                    "limit_tokens": int(stat_row.get("limit_tokens") or 0),
                    "remaining_tokens": int(stat_row.get("remaining_tokens") or 0),
                    "remaining_percentage": float(stat_row.get("remaining_percentage") or 0.0),
                    "used_percentage": float(stat_row.get("used_percentage") or 0.0),
                    "budget_action": str(stat_row.get("status") or "ok"),
                })
            prov = by_provider.setdefault(provider, {
                "provider": provider,
                "tokens_used": 0,
                "estimated_cost_usd": 0.0,
                "requests_count": 0,
                "models_tracked": 0,
            })
            prov["tokens_used"] += int(item.get("tokens_used") or 0)
            prov["estimated_cost_usd"] = round(float(prov["estimated_cost_usd"]) + float(item.get("estimated_cost_usd") or 0.0), 6)
            prov["requests_count"] += int(item.get("requests_count") or 0)
            prov["models_tracked"] += 1
        return by_model, by_provider

    def build_provider_endpoint_inventory(
        self,
        provider: str,
        *,
        force_refresh: bool = False,
        usage_snapshot: dict[str, Any] | None = None,
        suppression_snapshot: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        normalized = self._normalize_provider(provider)
        entry = self.refresh_provider_entry(normalized, force_refresh=True) if force_refresh else self.provider_snapshot(normalized)
        if not isinstance(entry, dict) or not entry:
            return {"provider": normalized, "status": "missing", "models": [], "summary": {"total_models": 0}}

        usage_by_model, usage_by_provider = self._usage_rows(usage_snapshot)
        suppression = suppression_snapshot.get(normalized) if isinstance(suppression_snapshot, dict) else None
        provider_status = self._classify_provider_error(entry)
        diagnostics = entry.get("diagnostics") or {}
        diagnostics = diagnostics if isinstance(diagnostics, dict) else {}
        runtime_inventory = (diagnostics.get("runtime_inventory") or {}) if isinstance(diagnostics, dict) else {}
        model_templates = runtime_inventory.get("model_templates") if isinstance(runtime_inventory, dict) else None
        model_templates = model_templates if isinstance(model_templates, dict) else ((entry.get("diagnostics") or {}).get("model_templates") if isinstance(entry.get("diagnostics"), dict) else None)
        rows: list[dict[str, Any]] = []
        resident_details = diagnostics.get("resident_details") if isinstance(diagnostics.get("resident_details"), list) else []
        resident_lookup = {
            str(item.get("name") or "").strip(): item
            for item in resident_details
            if isinstance(item, dict) and str(item.get("name") or "").strip()
        }
        if normalized == "openai" and isinstance(model_templates, dict) and isinstance(model_templates.get("models"), list):
            for row in model_templates.get("models") or []:
                if not isinstance(row, dict):
                    continue
                model_name = str(row.get("model_name") or "").strip()
                usage = usage_by_model.get((normalized, model_name), {})
                merged = dict(row)
                merged["usage"] = usage
                rows.append(merged)
        else:
            for model_name in entry.get("models") or []:
                usage = usage_by_model.get((normalized, str(model_name)), {})
                rows.append({
                    "model_name": str(model_name),
                    "status": "discovered" if bool(entry.get("ok")) else "provider_unavailable",
                    "kernel_eligible": bool(entry.get("ok")),
                    "fallback_candidate": bool(entry.get("ok")),
                    "usage": usage,
                })
        if normalized == "local_llm":
            for row in rows:
                model_name = str(row.get("model_name") or "").strip()
                resident_row = resident_lookup.get(model_name, {})
                size_vram = int(resident_row.get("size_vram") or 0) if isinstance(resident_row, dict) else 0
                row["resident"] = model_name in resident_lookup
                row["connected"] = model_name in resident_lookup
                row["gpu_resident"] = size_vram > 0
                row["size_vram"] = size_vram or None
                row["expires_at"] = resident_row.get("expires_at") if isinstance(resident_row, dict) else None
        status_index: dict[str, list[str]] = {}
        for row in rows:
            status_index.setdefault(str(row.get("status") or "unknown"), []).append(str(row.get("model_name") or ""))
        summary = {
            "total_models": len(rows),
            "eligible_models": sum(1 for row in rows if bool(row.get("kernel_eligible"))),
            "fallback_models": sum(1 for row in rows if bool(row.get("fallback_candidate"))),
            "blocked_models": sum(1 for row in rows if str(row.get("status") or "") in {"blocked", "provider_unavailable", "non_chat_incompatible", "probe_failed"}),
            "provider_status": provider_status,
        }
        if normalized == "local_llm":
            summary["resident_models"] = sum(1 for row in rows if bool(row.get("resident")))
            summary["connected_models"] = sum(1 for row in rows if bool(row.get("connected")))
            summary["gpu_resident_models"] = sum(1 for row in rows if bool(row.get("gpu_resident")))
        return {
            "provider": normalized,
            "status": provider_status,
            "suppressed": suppression is not None,
            "suppression": suppression,
            "source": entry.get("source"),
            "error": entry.get("error"),
            "status_code": entry.get("status_code"),
            "diagnostics": entry.get("diagnostics") or {},
            "summary": summary,
            "status_index": status_index,
            "usage": usage_by_provider.get(normalized, {"provider": normalized, "tokens_used": 0, "estimated_cost_usd": 0.0, "requests_count": 0, "models_tracked": 0}),
            "models": rows,
        }

    def build_all_provider_endpoint_inventories(
        self,
        *,
        force_refresh: bool = False,
        usage_snapshot: dict[str, Any] | None = None,
        suppression_snapshot: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        providers = ["openai", "mistral", "antigravity", "mimo", "local_llm", "ai_kernel"]
        inventories = {
            provider: self.build_provider_endpoint_inventory(
                provider,
                force_refresh=force_refresh,
                usage_snapshot=usage_snapshot,
                suppression_snapshot=suppression_snapshot,
            )
            for provider in providers
        }
        return {
            "generated_at": int(time.time()),
            "providers": inventories,
            "summary": {
                "provider_count": len(inventories),
                "healthy_or_discovered": sum(1 for item in inventories.values() if str(item.get("status") or "") in {"ready", "degraded", "unknown"}),
                "suppressed_count": sum(1 for item in inventories.values() if bool(item.get("suppressed"))),
            },
        }

    def build_provider_runtime_inventory(
        self,
        provider: str,
        *,
        force_refresh: bool = False,
        probe_limit: int | None = None,
        usage_snapshot: dict[str, Any] | None = None,
        suppression_snapshot: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        normalized = self._normalize_provider(provider)
        if normalized == "openai":
            runtime = self.refresh_openai_runtime_inventory(force_refresh=force_refresh, probe_limit=probe_limit)
            endpoint_inventory = self.build_provider_endpoint_inventory(
                normalized,
                force_refresh=True,
                usage_snapshot=usage_snapshot,
                suppression_snapshot=suppression_snapshot,
            )
            return {
                "provider": normalized,
                "provider_id": runtime.get("provider_id") or normalized,
                "provider_name": runtime.get("provider_name") or "OpenAI",
                "fetched_at": int(runtime.get("fetched_at") or time.time()),
                "status": endpoint_inventory.get("status"),
                "source": endpoint_inventory.get("source"),
                "suppressed": endpoint_inventory.get("suppressed", False),
                "suppression": endpoint_inventory.get("suppression"),
                "usage": endpoint_inventory.get("usage"),
                "summary": {
                    **dict(endpoint_inventory.get("summary") or {}),
                    "validated_models": int(runtime.get("validated_model_count") or 0),
                    "fully_routable_models": int(runtime.get("fully_routable_count") or 0),
                    "chat_ready_models": int(runtime.get("chat_ready_count") or 0),
                    "responses_ready_models": int(runtime.get("responses_ready_count") or 0),
                    "messages_ready_models": int(runtime.get("messages_ready_count") or 0),
                    "messages_count_tokens_ready_models": int(runtime.get("messages_count_tokens_ready_count") or 0),
                },
                "endpoints": {
                    "base_url": runtime.get("base_url"),
                    "models_endpoint": runtime.get("models_endpoint"),
                    "chat_completions_endpoint": runtime.get("chat_completions_endpoint"),
                    "responses_endpoint": runtime.get("responses_endpoint"),
                    "messages_endpoint": runtime.get("messages_endpoint"),
                    "messages_count_tokens_endpoint": runtime.get("messages_count_tokens_endpoint"),
                    "codex_endpoint": runtime.get("codex_endpoint"),
                },
                "endpoint_manifest": runtime.get("endpoint_manifest") or {},
                "recommended_models": runtime.get("recommended_models") or {},
                "pricing": runtime.get("pricing") or {},
                "models": endpoint_inventory.get("models") or [],
                "diagnostics": {
                    **dict(endpoint_inventory.get("diagnostics") or {}),
                    "endpoint_probe_summary": runtime.get("endpoint_probe_summary") or {},
                },
                "runtime": {
                    "selected_models": runtime.get("selected_models") or [],
                    "fully_routable_models": runtime.get("fully_routable_models") or [],
                    "chat_ready_models": runtime.get("chat_ready_models") or [],
                    "responses_ready_models": runtime.get("responses_ready_models") or [],
                    "messages_ready_models": runtime.get("messages_ready_models") or [],
                    "messages_count_tokens_ready_models": runtime.get("messages_count_tokens_ready_models") or [],
                    "validated_models": runtime.get("validated_models") or [],
                    "endpoint_probe_summary": runtime.get("endpoint_probe_summary") or {},
                    "model_templates": runtime.get("model_templates") or {},
                },
            }

        endpoint_inventory = self.build_provider_endpoint_inventory(
            normalized,
            force_refresh=force_refresh,
            usage_snapshot=usage_snapshot,
            suppression_snapshot=suppression_snapshot,
        )
        diagnostics = dict(endpoint_inventory.get("diagnostics") or {})
        models = []
        resident_details = diagnostics.get("resident_details") if isinstance(diagnostics.get("resident_details"), list) else []
        resident_lookup = {
            str(item.get("name") or "").strip(): item
            for item in resident_details
            if isinstance(item, dict) and str(item.get("name") or "").strip()
        }
        for row in endpoint_inventory.get("models") or []:
            model_name = str(row.get("model_name") or "").strip()
            enriched = dict(row)
            if normalized == "local_llm":
                resident_row = resident_lookup.get(model_name, {})
                size_vram = int(resident_row.get("size_vram") or 0) if isinstance(resident_row, dict) else 0
                enriched["resident"] = bool(row.get("resident"))
                enriched["connected"] = bool(row.get("connected"))
                enriched["gpu_resident"] = bool(row.get("gpu_resident")) or size_vram > 0
                enriched["size_vram"] = row.get("size_vram") or (size_vram or None)
                enriched["available"] = True
                enriched["service_reachable"] = endpoint_inventory.get("status") in {"ready", "degraded"}
            if normalized == "ai_kernel":
                alias = str(diagnostics.get("model_alias") or "").strip()
                connected = model_name == alias if alias else endpoint_inventory.get("status") == "ready"
                enriched["resident"] = connected
                enriched["connected"] = connected
                enriched["kernel_eligible"] = connected
                enriched["available"] = True
                enriched["service_reachable"] = endpoint_inventory.get("status") in {"ready", "degraded"}
            models.append(enriched)
        summary = dict(endpoint_inventory.get("summary") or {})
        if normalized == "local_llm":
            summary["resident_models"] = sum(1 for row in models if bool(row.get("resident")))
            summary["connected_models"] = sum(1 for row in models if bool(row.get("connected")))
            summary["gpu_resident_models"] = sum(1 for row in models if bool(row.get("gpu_resident")))
            summary["available_models"] = len(models)
        if normalized == "ai_kernel":
            summary["resident_models"] = sum(1 for row in models if bool(row.get("resident")))
            summary["connected_models"] = sum(1 for row in models if bool(row.get("connected")))
            summary["kernel_usable_models"] = sum(1 for row in models if bool(row.get("kernel_eligible")))
            summary["available_models"] = len(models)
        return {
            "provider": normalized,
            "fetched_at": int(time.time()),
            "status": endpoint_inventory.get("status"),
            "source": endpoint_inventory.get("source"),
            "suppressed": endpoint_inventory.get("suppressed", False),
            "suppression": endpoint_inventory.get("suppression"),
            "usage": endpoint_inventory.get("usage"),
            "summary": summary,
            "models": models,
            "diagnostics": diagnostics,
            "status_index": endpoint_inventory.get("status_index") or {},
            "error": endpoint_inventory.get("error"),
            "status_code": endpoint_inventory.get("status_code"),
            "endpoints": {
                "base_url": diagnostics.get("base_url") or diagnostics.get("configured_endpoint") or diagnostics.get("active_endpoint"),
                "active_endpoint": diagnostics.get("active_endpoint"),
                "candidate_endpoints": diagnostics.get("candidate_endpoints") or [],
            },
        }

    def build_all_provider_runtime_inventories(
        self,
        *,
        force_refresh: bool = False,
        probe_limit: int | None = None,
        usage_snapshot: dict[str, Any] | None = None,
        suppression_snapshot: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        providers = ["openai", "mistral", "antigravity", "mimo", "local_llm", "ai_kernel"]
        inventories = {
            provider: self.build_provider_runtime_inventory(
                provider,
                force_refresh=force_refresh,
                probe_limit=probe_limit,
                usage_snapshot=usage_snapshot,
                suppression_snapshot=suppression_snapshot,
            )
            for provider in providers
        }
        return {
            "generated_at": int(time.time()),
            "providers": inventories,
            "summary": {
                "provider_count": len(inventories),
                "ready_count": sum(1 for item in inventories.values() if str(item.get("status") or "") == "ready"),
                "degraded_count": sum(1 for item in inventories.values() if str(item.get("status") or "") == "degraded"),
                "suppressed_count": sum(1 for item in inventories.values() if bool(item.get("suppressed"))),
            },
        }

    def _entry_builders(self) -> dict[str, Any]:
        return {
            "openai": self._openai_entry,
            "antigravity": self._antigravity_entry,
            "mistral": self._mistral_entry,
            "mimo": self._mimo_entry,
            "local_llm": self._local_llm_entry,
            "ai_kernel": self._ai_kernel_entry,
        }

    @staticmethod
    def _entry_to_dict(entry: Any) -> dict[str, Any]:
        if isinstance(entry, dict):
            return dict(entry)
        try:
            return asdict(entry)
        except TypeError:
            return {
                "provider": getattr(entry, "provider", None),
                "fetched_at": getattr(entry, "fetched_at", None),
                "ok": getattr(entry, "ok", None),
                "source": getattr(entry, "source", None),
                "models": list(getattr(entry, "models", []) or []),
                "error": getattr(entry, "error", None),
                "status_code": getattr(entry, "status_code", None),
                "diagnostics": dict(getattr(entry, "diagnostics", {}) or {}),
            }

    def _index_payload(self, payload: dict[str, Any]) -> None:
        providers = {name: value for name, value in payload.items() if isinstance(value, dict) and str(value.get("provider") or "").strip()}
        self.model_index.rebuild(providers)
        self.model_index.persist()

    def collect(self, *, force_refresh: bool = False) -> dict[str, dict[str, Any]]:
        entries = {provider: builder(force_refresh=force_refresh) for provider, builder in self._entry_builders().items()}
        return {provider: self._entry_to_dict(entry) for provider, entry in entries.items()}

    def _entry_is_fresh(self, provider: str, entry: dict[str, Any]) -> bool:
        normalized = self._normalize_provider(provider)
        fetched_at = int(entry.get("fetched_at") or 0) if isinstance(entry, dict) else 0
        interval = int(self._entry_refresh_intervals_sec.get(normalized, self.snapshot_refresh_interval_sec) or self.snapshot_refresh_interval_sec)
        return bool(fetched_at) and (time.time() - fetched_at) < max(5, interval)

    def refresh_provider_entry(self, provider: str, *, force_refresh: bool = False) -> dict[str, Any]:
        normalized = self._normalize_provider(provider)
        builder = self._entry_builders().get(normalized)
        if builder is None:
            return {}
        snapshot = self._read_snapshot_file()
        providers = snapshot.get("providers") if isinstance(snapshot.get("providers"), dict) else {}
        cached = providers.get(normalized) if isinstance(providers, dict) else None
        if not force_refresh and isinstance(cached, dict) and self._entry_is_fresh(normalized, cached):
            return cached
        entry = self._entry_to_dict(builder(force_refresh=force_refresh))
        providers[normalized] = entry
        self.write_snapshot(providers)
        return entry

    def model_index_summary(self) -> dict[str, Any]:
        return self.model_index.snapshot()

    def find_model(self, model_name: str) -> dict[str, Any] | None:
        return self.model_index.find_model(model_name)

    def provider_models(self, provider: str) -> list[str]:
        return self.model_index.provider_models(provider)

    def write_snapshot(self, payload: dict[str, Any]) -> None:
        self.snapshot_path.parent.mkdir(parents=True, exist_ok=True)
        runtime_inventory = self._read_openai_runtime_inventory_cache()
        model_health = self.model_health.refresh(provider_snapshot={"providers": payload}, runtime_inventory=runtime_inventory)
        body = {"updated_at": int(time.time()), "providers": payload, "model_health": model_health}
        self.snapshot_path.write_text(json.dumps(body, ensure_ascii=True, indent=2), encoding="utf-8")
        self._index_payload(payload)

    def refresh(self, *, force_refresh: bool = False) -> dict[str, Any]:
        payload = self.collect(force_refresh=force_refresh)
        if self.mimo_auto_ping_enabled:
            try:
                payload["mimo_auto_ping"] = self.refresh_mimo_usable_snapshot(force_refresh=force_refresh)
            except Exception as exc:
                payload["mimo_auto_ping"] = {"status": "error", "error": str(exc)}
        self.write_snapshot(payload)
        return payload

    def read_snapshot(self) -> dict[str, Any]:
        payload = self._read_snapshot_file()
        if self._snapshot_is_stale(payload) and not self._testing_mode():
            try:
                refreshed = self.refresh(force_refresh=True)
                return {"updated_at": int(time.time()), "providers": refreshed, "model_health": self.model_health.load()}
            except Exception:
                providers = payload.get("providers", {}) if isinstance(payload, dict) else {}
                if isinstance(providers, dict):
                    self._index_payload(providers)
                return payload
        providers = payload.get("providers", {}) if isinstance(payload, dict) else {}
        if isinstance(providers, dict):
            self._index_payload(providers)
        return payload

    def provider_snapshot(self, provider: str) -> dict[str, Any]:
        payload = self.read_snapshot()
        providers = payload.get("providers", {})
        if isinstance(providers, dict):
            entry = providers.get(provider)
            if isinstance(entry, dict):
                return entry
        return {}

    @staticmethod
    def _row_reason(provider: str, row: dict[str, Any]) -> tuple[str, str]:
        error = str(row.get("error") or "").strip()
        skip_reason = str(row.get("skip_reason") or "").strip()
        lowered = error.lower()
        error_kind = str(row.get("error_kind") or "").strip().lower()
        if skip_reason:
            return skip_reason, "excluded_from_chat_routing"
        if "personal access tokens are not supported" in lowered:
            return "github_pat_not_supported", "use GitHub user/OAuth session instead of PAT or keep provider disabled"
        if error_kind == "auth_failed" or any(marker in lowered for marker in ("invalid api key", "unauthorized", "authentication required", "auth failed")) or row.get("status_code") == 401:
            return "auth_failed", "refresh provider credentials or keep the provider globally suppressed"
        if "labs model" in lowered or "labs_not_enabled" in lowered:
            return "labs_not_enabled", "enable the Labs model in Mistral organization settings or keep it excluded"
        if error_kind in {"unsupported_model", "model_unavailable"}:
            return error_kind, "remove the model from this endpoint route or keep it excluded from manifests"
        if error_kind == "rate_limited" or row.get("status_code") == 429:
            return "rate_limited", "reduce probe frequency or keep the route disabled until quota recovers"
        if error_kind == "messages_pool_unavailable":
            return "messages_pool_unavailable", "keep Claude messages routing disabled for this provider until the pool recovers"
        if "invalid model" in lowered or row.get("status_code") == 400:
            return "invalid_model", "remove the stale model id from routing allowlists and manifests"
        if row.get("status_code") == 403:
            return "forbidden", "fix provider entitlement or keep model excluded"
        if error_kind in {"upstream_unavailable", "upstream_error"}:
            return error_kind, "retry later and keep the provider endpoint out of critical routing until it stabilizes"
        return "probe_failed", "keep excluded until a ping returns usable text"

    def build_participation_snapshot(self, agent_records: list[Any] | None = None) -> dict[str, Any]:
        artifacts = self._ping_artifacts()
        model_ping = artifacts.get("model_ping", {})
        mimo_ping = artifacts.get("mimo_ping", {}) if self.mimo_enabled else {}
        mimo_usable = artifacts.get("mimo_usable", {}) if self.mimo_enabled else {}

        successful_direct: dict[str, set[str]] = {}
        active_now: list[dict[str, Any]] = []
        available: list[dict[str, Any]] = []
        unusable: list[dict[str, Any]] = []
        seen_active: set[tuple[str, str, str]] = set()
        seen_available: set[tuple[str, str, str]] = set()
        seen_unusable: set[tuple[str, str, str]] = set()

        def add(rows: list[dict[str, Any]], seen: set[tuple[str, str, str]], *, provider: str, model_name: str, source: str, reason: str | None = None, remediation: str | None = None, wired: bool | None = None) -> None:
            key = (provider, model_name, source)
            if not model_name or key in seen:
                return
            seen.add(key)
            entry = {"provider": provider, "model_name": model_name, "source": source}
            if reason:
                entry["reason"] = reason
            if remediation:
                entry["remediation"] = remediation
            if wired is not None:
                entry["wired"] = wired
            rows.append(entry)

        for provider_name, payload in model_ping.items():
            if not isinstance(payload, dict):
                continue
            provider = self._normalize_provider(provider_name)
            for row in payload.get("models", []):
                if not isinstance(row, dict):
                    continue
                model_name = str(row.get("model") or "").strip()
                if not model_name:
                    continue
                if row.get("ok"):
                    successful_direct.setdefault(provider, set()).add(model_name)
                else:
                    reason, remediation = self._row_reason(provider, row)
                    add(unusable, seen_unusable, provider=provider, model_name=model_name, source="direct_ping", reason=reason, remediation=remediation, wired=False)

        mimo_usable_set: set[str] = set()
        for row in mimo_usable.get("models", []):
            if not isinstance(row, dict):
                continue
            model_name = str(row.get("model") or "").strip()
            if not model_name:
                continue
            mimo_usable_set.add(model_name)
            provider = self._normalize_provider(model_name.split("/", 1)[0] if "/" in model_name else "mimo")
            add(active_now, seen_active, provider=provider, model_name=model_name, source="mimo_usable", wired=True)

        for row in mimo_ping.get("models", []):
            if not isinstance(row, dict) or row.get("ok"):
                continue
            model_name = str(row.get("model") or "").strip()
            provider = self._normalize_provider(model_name.split("/", 1)[0] if "/" in model_name else "mimo")
            reason, remediation = self._row_reason(provider, row)
            add(unusable, seen_unusable, provider=provider, model_name=model_name, source="mimo_ping", reason=reason, remediation=remediation, wired=False)

        registered_keys: set[tuple[str, str]] = set()
        for record in agent_records or []:
            provider = self._normalize_provider(getattr(record, "provider", ""))
            model_name = str(getattr(record, "model_name", "") or "").strip()
            if not provider or not model_name:
                continue
            registered_keys.add((provider, model_name))
            direct_ok = model_name in successful_direct.get(provider, set())
            mimo_ok = f"{provider}/{model_name}" in mimo_usable_set
            if direct_ok or mimo_ok:
                add(active_now, seen_active, provider=provider, model_name=model_name, source="registered_agent", wired=True)
            elif provider == "antigravity":
                add(unusable, seen_unusable, provider=provider, model_name=model_name, source="registered_agent", reason="direct_api_missing_or_unready", remediation="configure ANTIGRAVITY_API_KEY or keep Antigravity disabled", wired=True)

        for provider, models in successful_direct.items():
            for model_name in sorted(models):
                if (provider, model_name) in registered_keys:
                    continue
                if f"{provider}/{model_name}" in mimo_usable_set:
                    continue
                add(available, seen_available, provider=provider, model_name=model_name, source="direct_ping", wired=False)

        return {
            "active_now": active_now,
            "available_but_not_wired_directly": available,
            "present_but_unusable": unusable,
            "counts": {
                "active_now": len(active_now),
                "available_but_not_wired_directly": len(available),
                "present_but_unusable": len(unusable),
            },
        }
