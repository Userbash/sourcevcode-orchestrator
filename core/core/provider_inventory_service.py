from __future__ import annotations

import asyncio
import json
import os
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from core.core.gemini_model_registry import AntigravityModelRegistry
from core.core.mistral_model_registry import MistralModelRegistry
from core.core.openai_model_registry import OpenAIModelRegistry
from core.mimo.bridge import MimoAsyncBridge
import requests


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
    def __init__(self) -> None:
        self.snapshot_path = Path(os.getenv("PROVIDER_INVENTORY_SNAPSHOT_PATH", "core/.cache/provider_inventory_snapshot.json"))
        self.openai = OpenAIModelRegistry()
        self.antigravity = AntigravityModelRegistry()
        self.mistral = MistralModelRegistry()
        self.mimo_bridge = MimoAsyncBridge()

    @staticmethod
    def _normalize_provider(provider: str) -> str:
        raw = str(provider or "").strip().lower()
        if raw in {"google", "antigravity", "gemini", "agy"}:
            return "antigravity"
        if raw in {"local_llm", "ollama", "local"}:
            return "local"
        if raw in {"ai-kernel", "ai_kernel", "llama_cpp", "llama-cpp"}:
            return "ai_kernel"
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

    def _openai_entry(self, *, force_refresh: bool = False) -> ProviderInventoryEntry:
        models = self.openai.get_models(force_refresh=force_refresh)
        diag = self.openai.diagnostics()
        return ProviderInventoryEntry(
            provider="openai",
            fetched_at=int(time.time()),
            ok=bool(models),
            source=str(diag.get("source") or ("live" if force_refresh else "cache")),
            models=models,
            error=str(diag.get("error_message") or "") or None,
            diagnostics=diag,
        )

    def _antigravity_entry(self, *, force_refresh: bool = False) -> ProviderInventoryEntry:
        models = self.antigravity.get_models(force_refresh=force_refresh)
        return ProviderInventoryEntry(
            provider="antigravity",
            fetched_at=int(time.time()),
            ok=bool(models),
            source="registry",
            models=models,
            error=None if models else "inventory_unavailable",
            diagnostics={"cache_path": str(self.antigravity.cache_path), "ttl_sec": self.antigravity.ttl_sec},
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

    def _ai_kernel_entry(self, *, force_refresh: bool = False) -> ProviderInventoryEntry:
        base_url = (os.getenv("AI_KERNEL_BASE_URL") or "http://127.0.0.1:8012/v1").rstrip('/')
        api_key = os.getenv("AI_KERNEL_API_KEY") or 'local'
        alias = (os.getenv("AI_KERNEL_MODEL_ALIAS") or "hauhaucs-qwen36-35b-a3b-aggressive:q4_k_m").strip()
        models: list[str] = []
        error: str | None = None
        status_code: int | None = None
        try:
            response = requests.get(f"{base_url}/models", headers={"Authorization": f"Bearer {api_key}"}, timeout=5.0)
            status_code = response.status_code
            payload = response.json() if response.content else {}
            if isinstance(payload, dict):
                for item in payload.get('data', []) or []:
                    model_name = str(item.get('id') or '').strip()
                    if model_name:
                        models.append(model_name)
        except Exception as exc:
            error = str(exc)
        if not models and alias and not error:
            error = 'models_unavailable'
        return ProviderInventoryEntry(
            provider='ai_kernel',
            fetched_at=int(time.time()),
            ok=bool(models),
            source='openai_compatible',
            models=models,
            error=error,
            status_code=status_code,
            diagnostics={'base_url': base_url, 'model_alias': alias},
        )

    def _mimo_entry(self, *, force_refresh: bool = False) -> ProviderInventoryEntry:
        models: list[str] = []
        error: str | None = None
        snapshots = list(self.mimo_bridge.get_cached_models())
        loop_running = False
        try:
            asyncio.get_running_loop()
            loop_running = True
        except RuntimeError:
            loop_running = False
        if (force_refresh or not snapshots) and not loop_running:
            try:
                snapshots = asyncio.run(self.mimo_bridge.refresh_cache())
            except Exception as exc:
                snapshots = []
                error = str(exc)
        for item in snapshots:
            model_name = str(getattr(item, "full_id", "") or getattr(item, "id", "")).strip()
            if model_name:
                models.append(model_name)
        return ProviderInventoryEntry(
            provider="mimo",
            fetched_at=int(time.time()),
            ok=bool(models),
            source="bridge_cache" if loop_running or not force_refresh else "bridge_live",
            models=models,
            error=error,
            diagnostics={"cli_alive": bool(getattr(self.mimo_bridge, "is_cli_alive", False)), "loop_running": loop_running},
        )

    def collect(self, *, force_refresh: bool = False) -> dict[str, dict[str, Any]]:
        entries = {
            "openai": self._openai_entry(force_refresh=force_refresh),
            "antigravity": self._antigravity_entry(force_refresh=force_refresh),
            "mistral": self._mistral_entry(force_refresh=force_refresh),
            "mimo": self._mimo_entry(force_refresh=force_refresh),
            "ai_kernel": self._ai_kernel_entry(force_refresh=force_refresh),
        }
        return {provider: asdict(entry) for provider, entry in entries.items()}

    def write_snapshot(self, payload: dict[str, Any]) -> None:
        self.snapshot_path.parent.mkdir(parents=True, exist_ok=True)
        body = {"updated_at": int(time.time()), "providers": payload}
        self.snapshot_path.write_text(json.dumps(body, ensure_ascii=True, indent=2), encoding="utf-8")

    def refresh(self, *, force_refresh: bool = False) -> dict[str, Any]:
        payload = self.collect(force_refresh=force_refresh)
        self.write_snapshot(payload)
        return payload

    def read_snapshot(self) -> dict[str, Any]:
        if not self.snapshot_path.exists():
            return {"updated_at": None, "providers": {}}
        try:
            payload = json.loads(self.snapshot_path.read_text(encoding="utf-8"))
            if isinstance(payload, dict):
                providers = payload.get("providers")
                if isinstance(providers, dict):
                    return payload
        except Exception:
            pass
        return {"updated_at": None, "providers": {}}

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
        model = str(row.get("model") or "").strip()
        lowered = error.lower()
        if skip_reason:
            return skip_reason, "excluded_from_chat_routing"
        if "personal access tokens are not supported" in lowered:
            return "github_pat_not_supported", "use GitHub user/OAuth session instead of PAT or keep provider disabled"
        if "labs model" in lowered or "labs_not_enabled" in lowered:
            return "labs_not_enabled", "enable the Labs model in Mistral organization settings or keep it excluded"
        if "invalid model" in lowered or row.get("status_code") == 400:
            return "invalid_model", "remove the stale model id from routing allowlists and manifests"
        if row.get("status_code") == 403:
            return "forbidden", "fix provider entitlement or keep model excluded"
        return "probe_failed", "keep excluded until a ping returns usable text"

    def build_participation_snapshot(self, agent_records: list[Any] | None = None) -> dict[str, Any]:
        artifacts = self._ping_artifacts()
        model_ping = artifacts.get("model_ping", {})
        mimo_ping = artifacts.get("mimo_ping", {})
        mimo_usable = artifacts.get("mimo_usable", {})

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
                add(unusable, seen_unusable, provider=provider, model_name=model_name, source="registered_agent", reason="cli_missing_or_unready", remediation="install/configure agy or keep Antigravity disabled", wired=True)

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
