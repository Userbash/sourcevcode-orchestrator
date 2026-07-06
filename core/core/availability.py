from __future__ import annotations

import os
import shutil
import socket
import subprocess
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import Enum
from typing import Any
from urllib.error import URLError
from urllib.request import urlopen
from urllib.parse import urlsplit, urlunsplit

import requests

try:
    import httpx
except Exception:  # pragma: no cover - optional in minimal test envs
    httpx = None  # type: ignore

from .antigravity_model_registry import AntigravityModelRegistry
from .antigravity_runtime_router import AntigravityRuntimeRouter
from .env_loader import load_env_file
from .external_ai_bridge import ExternalAIBridge
from .provider_credentials import credential_snapshot
from .antigravity_provider import resolve_antigravity_provider_config
from .antigravity_status_module import shared_antigravity_snapshot
from .mimo_status import build_mimo_runtime_status, mimo_enabled
from .integrations.antigravity_manager import AntigravityManager
from .integrations.mistral_manager import MistralManager
from .openai_model_registry import OpenAIModelRegistry
from .provider_inventory_service import ProviderInventoryService
from .openai_provider import default_openai_tcp_probe_hosts, openai_endpoint_manifest, resolve_openai_provider_config


class ProviderStatus(Enum):
    HEALTHY = "healthy"
    DEGRADED = "degraded"
    TIMEOUT = "timeout"
    AUTH_FAILED = "auth_failed"
    QUOTA_EXCEEDED = "quota_exceeded"
    OFFLINE = "offline"


@dataclass(slots=True)
class ProviderHealth:
    provider: str
    status: ProviderStatus
    latency_ms: float
    last_check: datetime
    error: str | None = None
    diagnostics: dict[str, Any] = None

    def __post_init__(self) -> None:
        if self.diagnostics is None:
            self.diagnostics = {}

    def as_dict(self) -> dict:
        return {
            "provider": self.provider,
            "status": self.status.value,
            "latency_ms": self.latency_ms,
            "last_check": self.last_check.isoformat(),
            "error": self.error,
            "diagnostics": self.diagnostics,
        }


class ModelAvailability:
    @staticmethod
    def _endpoint_candidates(base_url: str) -> list[str]:
        raw = str(base_url or '').strip().rstrip('/')
        if not raw:
            return []
        candidates: list[str] = []

        def _push(url: str) -> None:
            item = url.rstrip('/')
            if item and item not in candidates:
                candidates.append(item)

        _push(raw)
        parsed = urlsplit(raw)
        if parsed.scheme and parsed.netloc:
            host = parsed.hostname or ''
            netloc = parsed.netloc
            variants: list[str] = []
            if host == '127.0.0.1':
                variants.extend(['host.containers.internal', 'localhost'])
            elif host == 'localhost':
                variants.extend(['127.0.0.1', 'host.containers.internal'])
            elif host == 'host.containers.internal':
                variants.extend(['127.0.0.1', 'localhost'])
            for variant in variants:
                swapped_netloc = netloc.replace(host, variant, 1)
                _push(urlunsplit((parsed.scheme, swapped_netloc, parsed.path, parsed.query, parsed.fragment)))
        return candidates

    @staticmethod
    def _normalize_provider(provider: str) -> str:
        p = provider.strip().lower()
        if p in {"antigravity", "antigravity-cli", "agy", "google", "gemini", "gemini-cli"}:
            return "antigravity"
        if p in {"openai", "codex", "codex-main", "gpt"}:
            return "openai"
        if p in {"mimo", "xiaomi", "mimo-cli"}:
            return "mimo"
        if p in {"ai_kernel", "ai-kernel", "llama_cpp", "llama-cpp"}:
            return "ai_kernel"
        return p

    def __init__(self) -> None:
        load_env_file()
        load_env_file(".env.bridge", override=True)
        load_env_file(".env.gemini.local", override=True)
        self._health_cache: dict[str, ProviderHealth] = {}
        self._failure_cache: dict[str, ProviderHealth] = {}
        self.inventory = ProviderInventoryService()

    def _snapshot_inventory(self, provider: str) -> dict[str, Any]:
        snapshot = self.inventory.provider_snapshot(provider)
        return snapshot if isinstance(snapshot, dict) else {}

    @staticmethod
    def _probe_timeout_sec() -> float:
        raw = os.getenv("AI_BRIDGE_PROVIDER_PROBE_TIMEOUT_SEC", "20").strip()
        try:
            return max(1.0, float(raw))
        except ValueError:
            return 5.0

    @staticmethod
    def _live_probe_enabled() -> bool:
        return os.getenv("AI_BRIDGE_LIVE_MODEL_PROBE", "true").strip().lower() in {"1", "true", "yes", "on"}

    @staticmethod
    def _antigravity_api_config() -> dict[str, str | bool]:
        cfg = resolve_antigravity_provider_config()
        return {
            "api_key_configured": bool(cfg.api_key),
            "base_url": cfg.base_url,
            "models_endpoint": cfg.models_endpoint,
            "chat_completions_endpoint": cfg.chat_completions_endpoint,
        }

    @staticmethod
    def _tcp_targets(provider: str) -> list[tuple[str, int]]:
        if provider == "antigravity":
            raw = os.getenv("ANTIGRAVITY_TCP_PROBE_HOSTS", os.getenv("GEMINI_TCP_PROBE_HOSTS", "antigravity.google:443,generativelanguage.googleapis.com:443,www.googleapis.com:443"))
        elif provider == "mistral":
            raw = os.getenv("MISTRAL_TCP_PROBE_HOSTS", "api.mistral.ai:443")
        elif provider == "openai":
            raw = os.getenv("OPENAI_TCP_PROBE_HOSTS", default_openai_tcp_probe_hosts())
        elif provider == "ai_kernel":
            raw = os.getenv("AI_KERNEL_TCP_PROBE_HOSTS", "127.0.0.1:8012")
        else:
            raw = ""

        targets: list[tuple[str, int]] = []
        for item in raw.split(","):
            host_port = item.strip()
            if not host_port:
                continue
            if ":" in host_port:
                host, port_raw = host_port.rsplit(":", 1)
            else:
                host, port_raw = host_port, "443"
            try:
                targets.append((host.strip(), int(port_raw)))
            except ValueError:
                continue
        return targets

    @classmethod
    def _tcp_probe(cls, provider: str) -> dict[str, Any]:
        timeout = cls._probe_timeout_sec()
        targets = cls._tcp_targets(provider)
        results: list[dict[str, Any]] = []
        if not targets:
            return {"ok": True, "skipped": True, "targets": results}

        for host, port in targets:
            started = datetime.now(UTC)
            try:
                with socket.create_connection((host, port), timeout=timeout):
                    latency = (datetime.now(UTC) - started).total_seconds() * 1000
                    results.append({"host": host, "port": port, "ok": True, "latency_ms": latency})
            except socket.timeout as exc:
                results.append({"host": host, "port": port, "ok": False, "error_type": "tcp_timeout", "error": str(exc) or "timeout"})
            except OSError as exc:
                results.append({"host": host, "port": port, "ok": False, "error_type": ExternalAIBridge.classify_error(str(exc)), "error": str(exc)})

        return {"ok": any(item.get("ok") for item in results), "targets": results}

    @staticmethod
    def _status_from_error(raw_error: str, default: ProviderStatus = ProviderStatus.OFFLINE) -> ProviderStatus:
        classified = ExternalAIBridge.classify_error(raw_error)
        if classified == "auth_fail":
            return ProviderStatus.AUTH_FAILED
        if classified == "quota_exhaustion":
            return ProviderStatus.QUOTA_EXCEEDED
        if classified in {"tcp_timeout", "api_timeout", "sdk_hang"}:
            return ProviderStatus.TIMEOUT
        return default

    @staticmethod
    def _antigravity_strategy_profiles() -> dict[str, list[str]]:
        return AntigravityRuntimeRouter.strategy_profiles()

    @staticmethod
    def _env_models(key: str) -> list[str]:
        raw = os.getenv(key, "").strip()
        if not raw:
            return []
        return [item.strip() for item in raw.split(",") if item.strip()]

    @staticmethod
    def _remediation(provider: str, status: ProviderStatus, diagnostics: dict[str, Any]) -> list[str]:
        steps: list[str] = []
        tcp = diagnostics.get("tcp", {}) if isinstance(diagnostics.get("tcp"), dict) else {}
        if status == ProviderStatus.AUTH_FAILED:
            if provider == "antigravity":
                key_name = "ANTIGRAVITY_API_KEY or ANTIGRAVITY_API_TOKEN"
            elif provider == "openai":
                key_name = "OPENAI_API_KEY"
            else:
                key_name = "MISTRAL_API_KEY"
            steps.append(f"Проверь {key_name}: переменная окружения или token-based endpoint должны быть заданы и не просрочены.")
        if status == ProviderStatus.QUOTA_EXCEEDED:
            steps.append("Проверь quota/rate limit у провайдера и временно снизь приоритет этого провайдера в routing policy.")
        if status in {ProviderStatus.TIMEOUT, ProviderStatus.OFFLINE}:
            steps.append("Проверь DNS и TCP egress из среды выполнения до provider API на 443/tcp.")
            steps.append("Проверь proxy/firewall/VPN: соединение должно открываться до host из tcp diagnostics.")
            if provider == "antigravity":
                steps.append("Проверь token-based HTTP/HTTPS endpoint и подтверждённый models/chat catalog для Antigravity.")
            if provider == "openai":
                steps.append("Проверь доступ к настроенному OpenAI-compatible endpoint `/models` и что выбранная Codex/OpenAI модель есть в live catalog.")
        if tcp and not tcp.get("ok"):
            steps.append("TCP probe не открыл ни одного соединения; fallback до другого провайдера корректен до восстановления сети.")
        return steps

    def _cache(self, health: ProviderHealth) -> ProviderHealth:
        provider = self._normalize_provider(health.provider)
        self._health_cache[provider] = health
        if health.status not in {ProviderStatus.HEALTHY, ProviderStatus.DEGRADED}:
            self._failure_cache[provider] = health
        else:
            self._failure_cache.pop(provider, None)
        return health

    def record_failure(self, provider: str, error_type: str, raw_error: str | None = None) -> ProviderHealth:
        normalized = self._normalize_provider(provider)
        status = self._status_from_error(error_type or raw_error or "", ProviderStatus.OFFLINE)
        health = ProviderHealth(
            normalized,
            status,
            0.0,
            datetime.now(UTC),
            error=raw_error or error_type,
            diagnostics={"error_type": error_type, "recorded": True},
        )
        return self._cache(health)

    def is_provider_ready(self, provider: str) -> bool:
        normalized = self._normalize_provider(provider)
        health = self._health_cache.get(normalized)
        if not health:
            return False
        return health.status == ProviderStatus.HEALTHY

    def check_antigravity(self, *, live: bool | None = None) -> ProviderHealth:
        start = datetime.now(UTC)
        diagnostics: dict[str, Any] = {
            "provider": "antigravity",
            "credential": credential_snapshot(("ANTIGRAVITY_API_KEY", "ANTIGRAVITY_API_TOKEN", "GEMINI_API_KEY", "GOOGLE_API_KEY")),
            "api_config": self._antigravity_api_config(),
        }
        tcp = self._tcp_probe("antigravity")
        diagnostics["tcp"] = tcp
        
        if not tcp.get("ok"):
            latency = (datetime.now(UTC) - start).total_seconds() * 1000
            health = ProviderHealth("antigravity", ProviderStatus.TIMEOUT, latency, datetime.now(UTC), error="tcp_probe_failed", diagnostics=diagnostics)
            diagnostics["remediation"] = self._remediation("antigravity", health.status, diagnostics)
            return self._cache(health)

        status = shared_antigravity_snapshot(force=False)
        manager = AntigravityManager()
        diagnostics["models"] = status.get("models", [])
        diagnostics["inventory_ok"] = status.get("inventory_ok")
        diagnostics["inventory_source"] = status.get("inventory_source")
        diagnostics["inventory_probe_kind"] = status.get("inventory_probe_kind")
        diagnostics["models_probe"] = status.get("models_probe", {})
        diagnostics["generation_probe"] = status.get("generation_probe", {})
        if status.get("auth_probe"):
            diagnostics["auth_probe"] = status.get("auth_probe")
        if status.get("api_probe"):
            diagnostics["api_probe"] = status.get("api_probe")
        diagnostics["auth_mode"] = status.get("auth_mode", "api_key")
        
        latency = (datetime.now(UTC) - start).total_seconds() * 1000
        
        if status.get("ready"):
            health = ProviderHealth("antigravity", ProviderStatus.HEALTHY, latency, datetime.now(UTC), diagnostics=diagnostics)
        else:
            raw_error = str((diagnostics.get("api_probe") or {}).get("error") or diagnostics.get("generation_probe", {}).get("stderr") or diagnostics.get("models_probe", {}).get("stderr") or (diagnostics.get("auth_probe") or {}).get("stderr") or "antigravity_not_ready")
            error = "antigravity_auth_failed" if self._status_from_error(raw_error, ProviderStatus.DEGRADED) == ProviderStatus.AUTH_FAILED else "antigravity_not_ready"
            health = ProviderHealth("antigravity", ProviderStatus.DEGRADED, latency, datetime.now(UTC), error=error, diagnostics=diagnostics)
            diagnostics["remediation"] = self._remediation("antigravity", health.status, diagnostics)
            
        return self._cache(health)

    def check_gemini(self, *, live: bool | None = None) -> ProviderHealth:
        # Legacy compatibility path retained for older call sites.
        return self.check_antigravity(live=live)

    def antigravity_status(self) -> dict[str, Any]:
        manager = AntigravityManager()
        try:
            snapshot = shared_antigravity_snapshot(force=False)
        except Exception:
            snapshot = manager.status()
        self._health_cache["antigravity"] = ProviderHealth(
            "antigravity",
            ProviderStatus.HEALTHY if snapshot.get("ready") else ProviderStatus.DEGRADED,
            0.0,
            datetime.now(UTC),
            error=None if snapshot.get("ready") else snapshot.get("error") or "antigravity_not_ready",
            diagnostics={"snapshot": snapshot},
        )
        return snapshot

    def check_mistral(self, *, live: bool | None = None) -> ProviderHealth:
        start = datetime.now(UTC)
        diagnostics: dict[str, Any] = {
            "provider": "mistral",
            "credential": credential_snapshot(("MISTRAL_API_KEY",)),
        }
        if not diagnostics["credential"].get("usable"):
            latency = (datetime.now(UTC) - start).total_seconds() * 1000
            error = "mistral_api_key_placeholder" if diagnostics["credential"].get("placeholder") else "mistral_api_key_missing"
            health = ProviderHealth("mistral", ProviderStatus.AUTH_FAILED, latency, datetime.now(UTC), error=error, diagnostics=diagnostics)
            diagnostics["remediation"] = self._remediation("mistral", health.status, diagnostics)
            return self._cache(health)

        # 1. TCP connectivity probe
        tcp = self._tcp_probe("mistral")
        diagnostics["tcp"] = tcp
        if not tcp.get("ok"):
            latency = (datetime.now(UTC) - start).total_seconds() * 1000
            health = ProviderHealth("mistral", ProviderStatus.TIMEOUT, latency, datetime.now(UTC), error="tcp_probe_failed", diagnostics=diagnostics)
            diagnostics["remediation"] = self._remediation("mistral", health.status, diagnostics)
            return self._cache(health)

        # 2. Functional/Auth probe using MistralManager
        manager = MistralManager()
        status = manager.status()
        diagnostics["models"] = status.get("models", [])
        diagnostics["api_probe"] = status.get("api_probe", {})
        diagnostics["registry"] = status.get("registry", {})
        diagnostics["inventory_source"] = status.get("inventory_source", "live")

        if not diagnostics["models"]:
            snapshot = self._snapshot_inventory("mistral")
            snap_models = [str(item).strip() for item in snapshot.get("models", []) if str(item).strip()] if isinstance(snapshot, dict) else []
            if snap_models:
                diagnostics["models"] = snap_models
                diagnostics["inventory_snapshot"] = snapshot
                diagnostics["inventory_source"] = "snapshot"

        latency = (datetime.now(UTC) - start).total_seconds() * 1000

        if status.get("ready") or diagnostics["models"]:
            provider_status = ProviderStatus.HEALTHY if status.get("ready") else ProviderStatus.DEGRADED
            health = ProviderHealth("mistral", provider_status, latency, datetime.now(UTC), diagnostics=diagnostics)
        else:
            error = "mistral_auth_failed" if not manager.api_key or diagnostics.get("api_probe", {}).get("status_code") in {401, 403} else "mistral_not_ready"
            health = ProviderHealth("mistral", ProviderStatus.DEGRADED, latency, datetime.now(UTC), error=error, diagnostics=diagnostics)
            diagnostics["remediation"] = self._remediation("mistral", health.status, diagnostics)

        return self._cache(health)


    def check_mimo(self, *, live: bool | None = None) -> ProviderHealth:
        start = datetime.now(UTC)
        diagnostics: dict[str, Any] = {
            "provider": "mimo",
            "credential": credential_snapshot(("MIMO_API_KEY", "AI_BRIDGE_MIMO_API_KEY")),
        }
        if not mimo_enabled():
            latency = (datetime.now(UTC) - start).total_seconds() * 1000
            diagnostics["disabled_by_env"] = True
            diagnostics["disable_env"] = "AI_BRIDGE_MIMO_ENABLED"
            return self._cache(ProviderHealth("mimo", ProviderStatus.OFFLINE, latency, datetime.now(UTC), error="mimo_disabled_by_env", diagnostics=diagnostics))
        snapshot = build_mimo_runtime_status()
        diagnostics["snapshot"] = snapshot
        inventory_snapshot = self._snapshot_inventory("mimo")
        if inventory_snapshot:
            diagnostics["inventory_snapshot"] = inventory_snapshot
        diagnostics["usable_models"] = snapshot.get("usable_models_sample", [])
        diagnostics["failed_models"] = snapshot.get("failed_models_sample", [])
        diagnostics["auth_categories"] = snapshot.get("auth_categories", {})
        diagnostics["provider_breakdown"] = snapshot.get("provider_breakdown", {})
        latency = (datetime.now(UTC) - start).total_seconds() * 1000

        if not snapshot.get("direct_api_configured") and not snapshot.get("report_present"):
            health = ProviderHealth("mimo", ProviderStatus.OFFLINE, latency, datetime.now(UTC), error="mimo_api_key_missing", diagnostics=diagnostics)
            return self._cache(health)

        if snapshot.get("ready"):
            status = ProviderStatus.HEALTHY if not snapshot.get("failed_count") else ProviderStatus.DEGRADED
            health = ProviderHealth("mimo", status, latency, datetime.now(UTC), diagnostics=diagnostics)
            if status != ProviderStatus.HEALTHY:
                diagnostics["remediation"] = [
                    "Часть MIMO моделей недоступна; используй usable_models_sample и auth_categories для routing policy.",
                    "Для GitHub Copilot-backed моделей проверь тип токена: PAT часто не поддерживается для run endpoint.",
                ]
            return self._cache(health)

        auth_categories = diagnostics.get("auth_categories") or {}
        if isinstance(auth_categories, dict) and any(key in auth_categories for key in {"invalid_api_key", "illegal_access", "token_plan_base_url_missing"}):
            health = ProviderHealth("mimo", ProviderStatus.AUTH_FAILED, latency, datetime.now(UTC), error="mimo_auth_degraded", diagnostics=diagnostics)
            diagnostics["remediation"] = [
                "Проверь MIMO_API_KEY и entitlement на native Xiaomi MIMO models.",
                "Если ключ формата tp-..., задай MIMO_BASE_URL/AI_BRIDGE_MIMO_BASE_URL из Token Plan page.",
            ]
            return self._cache(health)

        health = ProviderHealth("mimo", ProviderStatus.DEGRADED, latency, datetime.now(UTC), error="mimo_models_unavailable", diagnostics=diagnostics)
        diagnostics["remediation"] = [
            "Сними свежий sweep через core.scripts.ping_all_models и проверь failed_models_by_provider.json.",
            "Используй mimo_usable_models.json для allowlist моделей до восстановления остальных routes.",
        ]
        return self._cache(health)


    @staticmethod
    def _probe_openai_endpoint(name: str, url: str, api_key: str) -> dict[str, Any]:
        endpoint = str(url or "").strip()
        if not endpoint:
            return {"name": name, "url": endpoint, "ok": False, "error_type": "missing_endpoint", "status_code": None}
        try:
            response = requests.get(
                endpoint,
                headers={"Authorization": f"Bearer {api_key}"},
                timeout=5.0,
                allow_redirects=False,
            )
            status_code = int(response.status_code)
            if status_code == 200:
                return {"name": name, "url": endpoint, "ok": True, "status_code": status_code, "category": "ok"}
            if status_code == 405:
                return {"name": name, "url": endpoint, "ok": True, "status_code": status_code, "category": "method_not_allowed_but_present"}
            error_type = OpenAIModelRegistry._classify_status_code(status_code)[0]
            return {"name": name, "url": endpoint, "ok": False, "status_code": status_code, "error_type": error_type}
        except Exception as exc:
            return {
                "name": name,
                "url": endpoint,
                "ok": False,
                "status_code": None,
                "error_type": ExternalAIBridge.classify_error(str(exc)),
                "error": str(exc),
            }

    def check_openai(self, *, live: bool | None = None) -> ProviderHealth:
        start = datetime.now(UTC)
        cfg = resolve_openai_provider_config()
        endpoint_manifest = openai_endpoint_manifest(cfg)
        diagnostics: dict[str, Any] = {
            "provider": "openai",
            "credential": credential_snapshot(("OPENAI_API_KEY",)),
            "endpoint_manifest": endpoint_manifest,
        }
        if not diagnostics["credential"].get("usable"):
            latency = (datetime.now(UTC) - start).total_seconds() * 1000
            error = "openai_api_key_placeholder" if diagnostics["credential"].get("placeholder") else "openai_api_key_missing"
            health = ProviderHealth("openai", ProviderStatus.AUTH_FAILED, latency, datetime.now(UTC), error=error, diagnostics=diagnostics)
            diagnostics["remediation"] = self._remediation("openai", health.status, diagnostics)
            return self._cache(health)

        tcp = self._tcp_probe("openai")
        diagnostics["tcp"] = tcp
        if not tcp.get("ok"):
            latency = (datetime.now(UTC) - start).total_seconds() * 1000
            health = ProviderHealth("openai", ProviderStatus.TIMEOUT, latency, datetime.now(UTC), error="tcp_probe_failed", diagnostics=diagnostics)
            diagnostics["remediation"] = self._remediation("openai", health.status, diagnostics)
            return self._cache(health)

        endpoint_statuses: dict[str, dict[str, Any]] = {}
        for name, endpoint in cfg.endpoint_map().items():
            endpoint_statuses[name] = self._probe_openai_endpoint(name, endpoint, cfg.api_key)
        diagnostics["endpoint_statuses"] = endpoint_statuses

        registry = OpenAIModelRegistry()
        models = registry.get_models(force_refresh=bool(live if live is not None else self._live_probe_enabled()))
        diagnostics["models"] = models
        diagnostics["registry"] = registry.diagnostics()
        diagnostics["inventory_source"] = str((diagnostics.get("registry") or {}).get("source") or "live")
        configured = [
            os.getenv("CODEX_OPENAI_MODEL", "").strip(),
            *self._env_models("OPENAI_HIGH_MODELS"),
            *self._env_models("OPENAI_MEDIUM_MODELS"),
            *self._env_models("OPENAI_EXTRA_MODELS"),
        ]
        configured = [item for item in configured if item]
        diagnostics["configured_models"] = configured
        if configured and models:
            diagnostics["configured_models_available"] = [item for item in configured if item in set(models)]

        latency = (datetime.now(UTC) - start).total_seconds() * 1000
        models_probe = endpoint_statuses.get("models", {})
        if not models:
            snapshot = self._snapshot_inventory("openai")
            snap_models = [str(item).strip() for item in snapshot.get("models", []) if str(item).strip()] if isinstance(snapshot, dict) else []
            if snap_models:
                models = snap_models
                diagnostics["models"] = models
                diagnostics["inventory_snapshot"] = snapshot
                diagnostics["inventory_source"] = "snapshot"

        if models_probe.get("error_type") == "auth_fail":
            health = ProviderHealth("openai", ProviderStatus.AUTH_FAILED, latency, datetime.now(UTC), error="openai_auth_failed", diagnostics=diagnostics)
            diagnostics["remediation"] = self._remediation("openai", health.status, diagnostics)
            return self._cache(health)
        if models_probe.get("error_type") == "quota_exhaustion":
            health = ProviderHealth("openai", ProviderStatus.QUOTA_EXCEEDED, latency, datetime.now(UTC), error="openai_rate_limited", diagnostics=diagnostics)
            diagnostics["remediation"] = self._remediation("openai", health.status, diagnostics)
            return self._cache(health)
        if models_probe.get("error_type") in {"endpoint_unavailable", "api_timeout", "tcp_timeout"}:
            health = ProviderHealth("openai", ProviderStatus.TIMEOUT, latency, datetime.now(UTC), error="openai_endpoint_unavailable", diagnostics=diagnostics)
            diagnostics["remediation"] = self._remediation("openai", health.status, diagnostics)
            return self._cache(health)

        endpoint_failures = [name for name, payload in endpoint_statuses.items() if not payload.get("ok")]
        if models:
            status = ProviderStatus.HEALTHY if not endpoint_failures else ProviderStatus.DEGRADED
            health = ProviderHealth("openai", status, latency, datetime.now(UTC), diagnostics=diagnostics)
            if status != ProviderStatus.HEALTHY:
                diagnostics["remediation"] = [
                    "Часть OpenAI-compatible endpoints деградировала; ядро может продолжать routing через рабочие routes из endpoint_statuses.",
                    "При отказе chat/responses/messages переключай orchestration на local/mistral path и используй models snapshot для inventory.",
                ]
            return self._cache(health)

        health = ProviderHealth("openai", ProviderStatus.DEGRADED, latency, datetime.now(UTC), error="openai_models_unavailable", diagnostics=diagnostics)
        diagnostics["remediation"] = self._remediation("openai", health.status, diagnostics)
        return self._cache(health)

    def check_ai_kernel(self, *, live: bool | None = None) -> ProviderHealth:
        if os.getenv("AI_KERNEL_ENABLED", "true").strip().lower() not in {"1", "true", "yes", "on"}:
            return self._cache(ProviderHealth("ai_kernel", ProviderStatus.HEALTHY, 0.0, datetime.now(UTC), diagnostics={"provider": "ai_kernel", "enabled": False, "status": "disabled_by_env"}))
        start = datetime.now(UTC)
        base_url = (os.getenv("AI_KERNEL_BASE_URL") or "http://127.0.0.1:8012/v1").rstrip('/')
        diagnostics: dict[str, Any] = {
            "provider": "ai_kernel",
            "base_url": base_url,
            "model_alias": (os.getenv("AI_KERNEL_MODEL_ALIAS") or "hauhaucs-qwen36-35b-a3b-aggressive:q4_k_m").strip(),
        }
        diagnostics["candidate_base_urls"] = self._endpoint_candidates(base_url)
        tcp = self._tcp_probe("ai_kernel")
        diagnostics["tcp"] = tcp
        latency = (datetime.now(UTC) - start).total_seconds() * 1000
        if not tcp.get("ok"):
            health = ProviderHealth("ai_kernel", ProviderStatus.TIMEOUT, latency, datetime.now(UTC), error="tcp_probe_failed", diagnostics=diagnostics)
            return self._cache(health)
        try:
            last_error = "ai_kernel_models_unavailable"
            last_status_code: int | None = None
            for candidate_url in diagnostics["candidate_base_urls"]:
                try:
                    response = requests.get(f"{candidate_url}/models", headers={"Authorization": f"Bearer {os.getenv('AI_KERNEL_API_KEY', 'local')}"}, timeout=5.0)
                except Exception as exc:
                    last_error = f"{exc}@{candidate_url}"
                    continue
                last_status_code = response.status_code
                payload = response.json() if response.content else {}
                models = [str(item.get("id") or "").strip() for item in (payload.get("data") or []) if str(item.get("id") or "").strip()] if isinstance(payload, dict) else []
                diagnostics["status_code"] = response.status_code
                diagnostics["models"] = models
                diagnostics["resolved_base_url"] = candidate_url
                latency = (datetime.now(UTC) - start).total_seconds() * 1000
                if response.status_code == 200 and models:
                    diagnostics["base_url"] = candidate_url
                    return self._cache(ProviderHealth("ai_kernel", ProviderStatus.HEALTHY, latency, datetime.now(UTC), diagnostics=diagnostics))
                last_error = f"ai_kernel_status_{response.status_code}@{candidate_url}"
            latency = (datetime.now(UTC) - start).total_seconds() * 1000
            if last_status_code is not None:
                diagnostics["status_code"] = last_status_code
            status = ProviderStatus.OFFLINE if last_error and 'refused' in last_error.lower() else ProviderStatus.DEGRADED
            return self._cache(ProviderHealth("ai_kernel", status, latency, datetime.now(UTC), error=last_error, diagnostics=diagnostics))
        except Exception as exc:
            latency = (datetime.now(UTC) - start).total_seconds() * 1000
            return self._cache(ProviderHealth("ai_kernel", ProviderStatus.OFFLINE, latency, datetime.now(UTC), error=str(exc), diagnostics=diagnostics))

    def check_codex(self, *, live: bool | None = None) -> ProviderHealth:
        return self.check_openai(live=live)

    def check_provider(self, provider: str, *, live: bool | None = None) -> ProviderHealth:
        normalized = self._normalize_provider(provider)
        if normalized == "antigravity":
            return self.check_antigravity(live=live)
        if normalized == "mistral":
            return self.check_mistral(live=live)
        if normalized == "openai":
            return self.check_openai(live=live)
        if normalized == "mimo":
            return self.check_mimo(live=live)
        if normalized == "ai_kernel":
            return self.check_ai_kernel(live=live)
        health = ProviderHealth(normalized, ProviderStatus.HEALTHY, 0.0, datetime.now(UTC), diagnostics={"provider": normalized, "probe": "local_provider_assumed_ready"})
        return self._cache(health)

    def check_all(self) -> dict[str, ProviderHealth]:
        payload = {
            "antigravity": self.check_antigravity(),
            "mistral": self.check_mistral(),
            "openai": self.check_openai(),
            "mimo": self.check_mimo(),
        }
        if os.getenv("AI_KERNEL_ENABLED", "true").strip().lower() in {"1", "true", "yes", "on"}:
            payload["ai_kernel"] = self.check_ai_kernel()
        return payload

    def cached_report(self) -> dict[str, dict]:
        return {provider: health.as_dict() for provider, health in sorted(self._health_cache.items())}
