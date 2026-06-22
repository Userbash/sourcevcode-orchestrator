from __future__ import annotations

import asyncio
import concurrent.futures
import json
import os
import subprocess
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from core.core.gemini_model_registry import AntigravityModelRegistry
from core.core.mistral_model_registry import MistralModelRegistry
from core.core.openai_model_registry import OpenAIModelRegistry
from core.core.openai_compatible_inventory import is_text_compatible_model, sync_openai_compatible_artifacts
from core.core.openai_provider import resolve_openai_provider_config
from core.mimo.bridge import MimoAsyncBridge
from core.core.mimo_status import mimo_enabled
import httpx
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
        self.mimo_bridge = MimoAsyncBridge()
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

    @staticmethod
    def _normalize_provider(provider: str) -> str:
        raw = str(provider or "").strip().lower()
        if raw in {"google", "antigravity", "gemini", "agy"}:
            return "antigravity"
        if raw in {"local_llm", "ollama", "local"}:
            return "local"
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
        path = Path(os.getenv("OPENAI_MODELS_FULL_CACHE_PATH", "core/.cache/openai_models_full.json"))
        cache_models: list[str] = []
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            rows = payload.get("models") if isinstance(payload, dict) else []
            if isinstance(rows, list):
                cache_models.extend(str(item).strip() for item in rows if str(item).strip())
        except Exception:
            pass

        # Prefer the explicit cache when it already contains MIMO rows. Pulling in
        # the generated catalog on top of a live cache makes restart-time inventory
        # drift and broke the inventory contract in tests.
        if any(str(model).lower().startswith("mimo-") for model in cache_models):
            models = cache_models
        else:
            models = []

        generated_root = Path(os.getenv("OPENAI_GENERATED_PROFILE_DIR", "core/mimo/profiles/generated/openai_compatible"))
        manifest_path = generated_root / "manifest.json"
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except Exception:
            manifest = {}

        if not models:
            manifest_models = manifest.get("models") if isinstance(manifest, dict) else []
            if isinstance(manifest_models, list):
                models.extend(str(item).strip() for item in manifest_models if str(item).strip())

        model_profiles = manifest.get("model_profiles") if isinstance(manifest, dict) else []
        if not models:
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
                if model_name and (family == "mimo" or model_name.lower().startswith("mimo-")):
                    models.append(model_name)

        seen: set[str] = set()
        generated: list[str] = []
        for model in models:
            lowered = model.lower()
            if not lowered.startswith("mimo-") or model in seen:
                continue
            seen.add(model)
            generated.append(model)
        return generated

    @classmethod
    def _inventory_source_paths(cls) -> list[Path]:
        report_dir = cls._report_dir()
        paths = [
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

    async def _probe_openai_runtime_matrix_async(
        self,
        *,
        model_names: list[str],
        api_key: str,
        chat_endpoint: str,
        responses_endpoint: str,
        prompt: str,
        max_parallel: int,
    ) -> list[dict[str, Any]]:
        timeout = httpx.Timeout(30.0)
        semaphore = asyncio.Semaphore(max(1, max_parallel))
        headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}

        async with httpx.AsyncClient(timeout=timeout) as client:
            async def _call(endpoint_name: str, endpoint_url: str, payload: dict[str, Any]) -> dict[str, Any]:
                async with semaphore:
                    try:
                        response = await client.post(endpoint_url, headers=headers, json=payload)
                    except Exception as exc:
                        return {"ok": False, "status_code": None, "error": str(exc), "response_sample": "", "endpoint": endpoint_name}
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
                        }
                    return {
                        "ok": False,
                        "status_code": int(response.status_code),
                        "error": str(response.text[:240]),
                        "response_sample": "",
                        "endpoint": endpoint_name,
                    }

            async def _probe_model(model_name: str) -> dict[str, Any]:
                chat_payload = {"model": model_name, "messages": [{"role": "user", "content": prompt}], "max_tokens": 8}
                responses_payload = {"model": model_name, "input": prompt, "max_output_tokens": 8}
                chat_result, responses_result = await asyncio.gather(
                    _call("chat_completions", chat_endpoint, chat_payload),
                    _call("responses", responses_endpoint, responses_payload),
                )
                return {
                    "model": model_name,
                    "chat_completions": chat_result,
                    "responses": responses_result,
                    "fully_routable": bool(chat_result.get("ok") and responses_result.get("ok")),
                }

            return await asyncio.gather(*(_probe_model(model_name) for model_name in model_names))

    def refresh_openai_runtime_inventory(self, *, force_refresh: bool = False, probe_limit: int | None = None) -> dict[str, Any]:
        models = self.openai.get_models(force_refresh=force_refresh)
        diagnostics = dict(self.openai.diagnostics())
        cfg = resolve_openai_provider_config()
        sync_summary: dict[str, Any] = {}
        if models:
            try:
                sync_summary = sync_openai_compatible_artifacts(models, base_url=str(cfg.base_url or "").strip())
            except Exception as exc:
                sync_summary = {"ok": False, "error": str(exc)}

        selected = self._select_openai_probe_models(
            models,
            default_model=str(getattr(cfg, "default_model", "") or ""),
            limit=self._openai_probe_limit() if probe_limit is None else int(probe_limit),
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
                    prompt=self._openai_probe_prompt(),
                    max_parallel=max(2, min(16, len(selected) * 2)),
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

        fully_routable = [row for row in probe_rows if bool(row.get("fully_routable"))]
        chat_ready = [row for row in probe_rows if bool(((row.get("chat_completions") or {}).get("ok")))]
        responses_ready = [row for row in probe_rows if bool(((row.get("responses") or {}).get("ok")))]
        payload = {
            "provider": "openai",
            "fetched_at": int(time.time()),
            "force_refresh": bool(force_refresh),
            "registry_diagnostics": diagnostics,
            "models": models,
            "model_count": len(models),
            "selected_models": selected,
            "selected_model_count": len(selected),
            "validated_models": probe_rows,
            "validated_model_count": len(probe_rows),
            "fully_routable_models": [str(row.get("model") or "") for row in fully_routable],
            "fully_routable_count": len(fully_routable),
            "chat_ready_count": len(chat_ready),
            "responses_ready_count": len(responses_ready),
            "artifact_sync": sync_summary,
            "base_url": str(getattr(cfg, "base_url", "") or ""),
            "models_endpoint": str(getattr(cfg, "models_endpoint", "") or ""),
            "chat_completions_endpoint": str(getattr(cfg, "chat_completions_endpoint", "") or ""),
            "responses_endpoint": str(getattr(cfg, "responses_endpoint", "") or ""),
            "default_model": str(getattr(cfg, "default_model", "") or ""),
            "execution_mode": execution_mode,
        }
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
            limit = max(1, int(os.getenv('AI_BRIDGE_MIMO_AUTO_PING_LIMIT', '6') or '6'))
        except ValueError:
            limit = 6
        unique: list[str] = []
        seen: set[str] = set()
        def add(candidate: str) -> None:
            if candidate and candidate not in seen:
                unique.append(candidate)
                seen.add(candidate)
        preferred_prefixes = ('xiaomi/mimo-v2.5-pro', 'xiaomi/mimo-v2.5', 'xiaomi/mimo-v2-pro', 'xiaomi/mimo-v2-omni', 'mimo/mimo-auto')
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
            if len(unique) >= limit:
                break
        return unique[:limit]

    def refresh_mimo_usable_snapshot(self, *, force_refresh: bool = False, prompt: str = 'reply with pong only') -> dict[str, Any]:
        if not self.mimo_enabled:
            return {'status': 'disabled', 'reason': 'mimo_disabled_by_env', 'disable_env': 'AI_BRIDGE_MIMO_ENABLED'}
        report_dir = self._report_dir()
        report_dir.mkdir(parents=True, exist_ok=True)
        now = time.time()
        if not force_refresh and self._last_mimo_auto_ping_at and now - self._last_mimo_auto_ping_at < self.mimo_auto_ping_interval_sec:
            return {
                'status': 'skipped',
                'reason': 'interval_not_elapsed',
                'next_refresh_in_sec': max(0, int(self.mimo_auto_ping_interval_sec - (now - self._last_mimo_auto_ping_at))),
            }
        snapshots = self.mimo_bridge.refresh_cache_sync()
        inventory = []
        for item in snapshots:
            model_name = str(getattr(item, 'full_id', '') or getattr(item, 'id', '')).strip()
            if model_name:
                inventory.append(model_name)
        if not inventory:
            generated = self._generated_mimo_models()
            inventory = [f'xiaomi/{name}' if '/' not in name else name for name in generated]
        selected = self._select_mimo_probe_models(inventory)
        rows: list[dict[str, Any]] = []
        usable_rows: list[dict[str, Any]] = []
        for model_name in selected:
            row: dict[str, Any] = {'model': model_name}
            try:
                proc = subprocess.run(
                    ['timeout', '15s', 'mimo', 'run', '-m', model_name, '--format', 'json', prompt],
                    capture_output=True,
                    text=True,
                    timeout=25,
                    check=False,
                )
                text_output = self._parse_mimo_text_events(proc.stdout)
                row['exit_code'] = proc.returncode
                if text_output:
                    row['ok'] = True
                    row['response_sample'] = text_output[:120]
                    usable_rows.append({'model': model_name, 'ok': True, 'response_sample': text_output[:120], 'exit_code': proc.returncode})
                else:
                    row['ok'] = False
                    row['error'] = (proc.stderr or proc.stdout or ('timeout' if proc.returncode == 124 else 'no_text_events')).strip()[:240]
            except Exception as exc:
                row['ok'] = False
                row['error'] = str(exc)
            rows.append(row)
        report_payload = {
            'provider': 'mimo',
            'generated_at': int(now),
            'inventory_count': len(inventory),
            'probed_count': len(selected),
            'ok': sum(1 for row in rows if row.get('ok')),
            'failed': sum(1 for row in rows if not row.get('ok')),
            'models': rows,
        }
        usable_payload = {
            'provider': 'mimo',
            'generated_at': int(now),
            'usable_count': len(usable_rows),
            'total': len(selected),
            'models': usable_rows,
        }
        (report_dir / 'mimo_model_ping_report.json').write_text(json.dumps(report_payload, ensure_ascii=True, indent=2), encoding='utf-8')
        (report_dir / 'mimo_usable_models.json').write_text(json.dumps(usable_payload, ensure_ascii=True, indent=2), encoding='utf-8')
        self._last_mimo_auto_ping_at = now
        return {'status': 'ok', 'inventory_count': len(inventory), 'probed_count': len(selected), 'usable_count': len(usable_rows), 'report_dir': str(report_dir)}

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
        if not self.mimo_enabled:
            return ProviderInventoryEntry(
                provider="mimo",
                fetched_at=int(time.time()),
                ok=False,
                source="disabled_by_env",
                models=[],
                error="mimo_disabled_by_env",
                diagnostics={"enabled": False, "disable_env": "AI_BRIDGE_MIMO_ENABLED"},
            )
        models: list[str] = []
        error: str | None = None
        source = "bridge_cache"
        snapshots = list(self.mimo_bridge.get_cached_models())
        loop_running = False
        try:
            asyncio.get_running_loop()
            loop_running = True
        except RuntimeError:
            loop_running = False

        if force_refresh or not snapshots:
            try:
                snapshots = self.mimo_bridge.refresh_cache_sync()
                source = "bridge_live_sync" if force_refresh else "bridge_bootstrap_sync"
            except Exception as exc:
                snapshots = []
                error = str(exc)
        elif snapshots:
            source = "bridge_cache"

        for item in snapshots:
            model_name = str(getattr(item, "full_id", "") or getattr(item, "id", "")).strip()
            if model_name:
                models.append(model_name)

        artifact_models = self._artifact_mimo_models()
        usable_models = artifact_models["usable"]
        ping_models = artifact_models["ping"]
        generated_models = self._generated_mimo_models()
        fallback_used = False
        if not models and generated_models:
            models = list(generated_models)
            source = "generated_manifest_fallback"
            fallback_used = True
            if not error:
                error = "live_inventory_unavailable_using_generated_manifest"
        else:
            generated_models = [f"xiaomi/{name}" if "/" not in name else name for name in generated_models]

        models = self._merge_model_names(models, usable_models, ping_models)

        if source != "generated_manifest_fallback" and (usable_models or ping_models):
            source = f"{source}+artifact_merge"

        return ProviderInventoryEntry(
            provider="mimo",
            fetched_at=int(time.time()),
            ok=bool(snapshots),
            source=source,
            models=models,
            error=error,
            diagnostics={
                "cli_alive": bool(getattr(self.mimo_bridge, "is_cli_alive", False)),
                "loop_running": loop_running,
                "generated_fallback_used": fallback_used,
                "generated_models_count": len(generated_models),
                "usable_artifact_models_count": len(usable_models),
                "ping_artifact_models_count": len(ping_models),
                "auto_added_models_count": max(0, len(models) - len({
                    str(getattr(item, "full_id", "") or getattr(item, "id", "")).strip()
                    for item in snapshots
                    if str(getattr(item, "full_id", "") or getattr(item, "id", "")).strip()
                })),
            },
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
        if self.mimo_auto_ping_enabled:
            try:
                payload["mimo_auto_ping"] = self.refresh_mimo_usable_snapshot(force_refresh=force_refresh)
            except Exception as exc:
                payload["mimo_auto_ping"] = {"status": "error", "error": str(exc)}
        self.write_snapshot(payload)
        return payload

    def read_snapshot(self) -> dict[str, Any]:
        payload = self._read_snapshot_file()
        if self._snapshot_is_stale(payload):
            try:
                return {"updated_at": int(time.time()), "providers": self.refresh(force_refresh=True)}
            except Exception:
                return payload
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
        model = str(row.get("model") or "").strip()
        lowered = error.lower()
        if skip_reason:
            return skip_reason, "excluded_from_chat_routing"
        if "personal access tokens are not supported" in lowered:
            return "github_pat_not_supported", "use GitHub user/OAuth session instead of PAT or keep provider disabled"
        if any(marker in lowered for marker in ("invalid api key", "unauthorized", "authentication required", "auth failed")) or row.get("status_code") == 401:
            return "auth_failed", "refresh provider credentials or keep the provider globally suppressed"
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
