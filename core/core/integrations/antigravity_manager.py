from __future__ import annotations

import logging
import os
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import httpx

from core.core.antigravity_provider import (
    antigravity_request_headers,
    extract_antigravity_response_text,
    resolve_antigravity_model_alias,
    resolve_antigravity_provider_config,
)
from core.core.env_loader import load_env_file
from core.core.antigravity_model_registry import AntigravityModelRegistry
from core.core.host_bridge import HostBridge
from .antigravity_session_store import AntigravitySessionStore

logger = logging.getLogger("AntigravityManager")


class AntigravityManager:
    def __init__(self, *, host_bridge: HostBridge | None = None) -> None:
        load_env_file()
        load_env_file(".env.bridge", override=True)
        load_env_file(".env.gemini.local", override=True)
        self.host_bridge = host_bridge or HostBridge()
        self.probe_timeout = self._read_int("AI_BRIDGE_ANTIGRAVITY_PROBE_TIMEOUT_SEC", 30)
        self.login_timeout = self._read_int("AI_BRIDGE_ANTIGRAVITY_LOGIN_TIMEOUT_SEC", 60)
        self.api_key = (
            os.getenv("ANTIGRAVITY_API_KEY")
            or os.getenv("GEMINI_API_KEY")
            or os.getenv("GOOGLE_API_KEY")
            or ""
        ).strip()
        cfg = resolve_antigravity_provider_config()
        self.api_base_url = self._normalize_base_url(
            os.getenv("AI_BRIDGE_ANTIGRAVITY_API_BASE_URL", cfg.base_url)
        )
        self.models_endpoint = cfg.models_endpoint
        self.chat_completions_endpoint = cfg.chat_completions_endpoint
        self.default_model = cfg.default_model
        self.proxy_url = self._normalize_base_url(os.getenv("AI_BRIDGE_ANTIGRAVITY_PROXY_URL", ""))
        self.session_store = AntigravitySessionStore()
        self.registry = AntigravityModelRegistry()

    @staticmethod
    def _normalize_base_url(url: str) -> str:
        raw = str(url or "").strip().rstrip("/")
        if not raw:
            return ""
        parsed = urlsplit(raw)
        if parsed.scheme == "ws":
            return urlunsplit(parsed._replace(scheme="http"))
        if parsed.scheme == "wss":
            return urlunsplit(parsed._replace(scheme="https"))
        return raw

    def _endpoint_base_url(self) -> str:
        return self.proxy_url or self.api_base_url

    @staticmethod
    def _read_int(key: str, default: int) -> int:
        raw = os.getenv(key, str(default)).strip()
        try:
            return max(5, int(raw))
        except ValueError:
            return default

    @staticmethod
    def auto_login_enabled() -> bool:
        return os.getenv("AI_BRIDGE_ANTIGRAVITY_AUTO_LOGIN", "false").strip().lower() in {"1", "true", "yes", "on"}

    @staticmethod
    def _login_failure_cooldown_sec() -> int:
        raw = os.getenv("AI_BRIDGE_ANTIGRAVITY_LOGIN_FAILURE_COOLDOWN_SEC", "900").strip()
        try:
            return max(60, int(raw))
        except ValueError:
            return 900

    @staticmethod
    def _interactive_login_cooldown_sec() -> int:
        raw = os.getenv("AI_BRIDGE_ANTIGRAVITY_INTERACTIVE_LOGIN_COOLDOWN_SEC", "300").strip()
        try:
            return max(60, int(raw))
        except ValueError:
            return 300

    @staticmethod
    def _recent_session_grace_sec() -> int:
        raw = os.getenv("AI_BRIDGE_ANTIGRAVITY_SESSION_GRACE_SEC", "43200").strip()
        try:
            return max(300, int(raw))
        except ValueError:
            return 43200

    @staticmethod
    def _classify_failure_text(raw: str) -> str:
        text = str(raw or "").strip().lower()
        if not text:
            return "unknown"
        if any(marker in text for marker in ["missing_api_key", "api key required", "no api key"]):
            return "missing_api_key"
        if any(marker in text for marker in ["not found", "no such file", "cli missing", "command not found"]):
            return "cli_missing"
        if any(marker in text for marker in ["unsupported_client", "ineligibletiererror", "migrate to the antigravity suite of products"]):
            return "unsupported_client"
        if any(marker in text for marker in ["authentication required", "please sign in", "authorization code", "paste the authorization code", "unauthorized", "forbidden", "error: authentication", "error: please sign in"]):
            return "auth_required"
        if any(marker in text for marker in ["timed out", "timeout", "connection reset", "temporarily unavailable", "network", "dns", "econn", "refused", "unreachable"]):
            return "transient"
        return "unknown"

    def _annotate_verify_result(self, result: dict[str, Any]) -> dict[str, Any]:
        payload = dict(result or {})
        stderr = str(payload.get("stderr") or payload.get("error") or payload.get("stdout") or "")
        payload.setdefault("failure_kind", self._classify_failure_text(stderr))
        payload.setdefault("auth_marker_present", self.session_store.auth_marker_present())
        return payload

    def _request_json(self, method: str, path: str, *, timeout: float | None = None, json_body: dict[str, Any] | None = None, params: dict[str, Any] | None = None) -> httpx.Response:
        base_url = self._endpoint_base_url()
        if not base_url:
            raise RuntimeError("antigravity_api_base_url_missing")
        headers = antigravity_request_headers(self.api_key) if self.api_key else {}
        query: dict[str, Any] = dict(params or {})
        url = f"{base_url.rstrip('/')}/{path.lstrip('/')}"
        return httpx.request(method, url, params=query or None, json=json_body, headers=headers or None, timeout=timeout or self.probe_timeout)

    @staticmethod
    def _models_from_payload(payload: Any) -> list[str]:
        models: list[str] = []
        if isinstance(payload, dict):
            rows = payload.get("models", [])
            if isinstance(rows, list):
                for item in rows:
                    if isinstance(item, dict):
                        name = str(item.get("name") or item.get("id") or item.get("model") or "").strip()
                    else:
                        name = str(item).strip()
                    if name:
                        models.append(name.rsplit("/", 1)[-1])
            elif isinstance(rows, str):
                models.extend(line.strip() for line in rows.splitlines() if line.strip())
        seen: set[str] = set()
        deduped: list[str] = []
        for model in models:
            cleaned = str(model).strip()
            if not cleaned or cleaned in seen:
                continue
            seen.add(cleaned)
            deduped.append(cleaned)
        return deduped

    @staticmethod
    def _generation_text_from_payload(payload: Any) -> str:
        return extract_antigravity_response_text(payload)

    def _probe_generation(self, model_name: str | None = None) -> dict[str, Any]:
        prompt = "healthcheck: reply with ok"
        base_url = self._endpoint_base_url()
        if not base_url:
            return {"ok": False, "stdout": "", "stderr": "antigravity_api_base_url_missing", "error": "antigravity_api_base_url_missing", "status_code": None, "auth_mode": "api_key"}
        if not self.api_key:
            return {"ok": False, "stdout": "", "stderr": "missing_api_key", "error": "missing_api_key", "status_code": None, "auth_mode": "api_key"}
        timeout = max(self.probe_timeout, 10)
        try:
            if self.proxy_url:
                response = self._request_json("POST", "prompt", timeout=timeout + 10, json_body={"prompt": prompt, "timeout_sec": self.probe_timeout})
                payload = response.json() if response.content else {}
                stdout = str(payload.get("stdout") or self._generation_text_from_payload(payload) or "").strip()
                stderr = str(payload.get("stderr") or payload.get("error") or "").strip()
                ok = bool(payload.get("ok") if isinstance(payload, dict) else response.status_code == 200)
                return {"ok": ok, "status_code": response.status_code, "stdout": stdout, "stderr": stderr, "error": None if ok else (stderr or response.text[:500]), "auth_mode": "api_key", "model": model_name or "proxy"}

            chosen_model = resolve_antigravity_model_alias(model_name or self.default_model)
            response = httpx.post(
                self.chat_completions_endpoint,
                headers=antigravity_request_headers(self.api_key),
                json={
                    "model": chosen_model,
                    "messages": [{"role": "user", "content": prompt}],
                    "max_completion_tokens": 32,
                    "temperature": 0.0,
                    "stream": False,
                },
                timeout=timeout + 10,
            )
            payload = response.json() if response.content else {}
            stdout = self._generation_text_from_payload(payload)
            ok = response.status_code == 200 and bool(stdout or str(payload).strip())
            stderr = "" if ok else response.text[:500]
            return {"ok": ok, "status_code": response.status_code, "stdout": stdout, "stderr": stderr, "error": None if ok else stderr, "auth_mode": "api_key", "model": chosen_model}
        except Exception as exc:
            return {"ok": False, "status_code": None, "stdout": "", "stderr": str(exc), "error": str(exc), "auth_mode": "api_key", "model": model_name or "unknown"}

    def _active_interactive_session(self) -> dict[str, Any] | None:
        session = self.session_store.load_interactive_session()
        if not session.get("session_id"):
            return None
        if not self.session_store.interactive_session_active(session_id=str(session.get("session_id") or "")):
            return None
        return session

    def _pending_session_response(self, verify: dict[str, Any], session: dict[str, Any], *, reason: str) -> dict[str, Any]:
        payload = dict(verify or {})
        payload["ok"] = False
        payload["login_suppressed"] = True
        payload["suppression_reason"] = reason
        payload["interactive_session"] = session
        payload["action"] = "login_pending"
        return payload

    def _maybe_suppress_login(self, verify: dict[str, Any]) -> dict[str, Any] | None:
        active_session = self._active_interactive_session()
        if active_session is not None:
            return self._pending_session_response(verify, active_session, reason="interactive_session_active")
        failure_kind = str(verify.get("failure_kind") or "unknown")
        if self.session_store.login_failure_cooldown_active(cooldown_sec=self._login_failure_cooldown_sec()):
            verify["login_suppressed"] = True
            verify["suppression_reason"] = "login_failure_cooldown"
            return verify
        if failure_kind == "auth_required" and self.session_store.login_started_cooldown_active(cooldown_sec=self._interactive_login_cooldown_sec()):
            verify["login_suppressed"] = True
            verify["suppression_reason"] = "interactive_login_recently_started"
            return verify
        if failure_kind in {"transient", "unknown"} and bool(verify.get("auth_marker_present")) and self.session_store.recently_verified(within_sec=self._recent_session_grace_sec()):
            verify["login_suppressed"] = True
            verify["suppression_reason"] = "cached_session_recently_verified"
            return verify
        return None

    def interactive_session_status(self, session_id: str | None = None) -> dict[str, Any]:
        session = self.session_store.load_interactive_session(session_id)
        session["active"] = self.session_store.interactive_session_active(session_id=str(session.get("session_id") or session_id or "")) if (session.get("session_id") or session_id) else False
        return session

    def submit_interactive_input(self, text: str, session_id: str | None = None) -> dict[str, Any]:
        target_session_id = str(session_id or self.session_store.load().get("interactive_session_id") or "").strip()
        if not target_session_id:
            return {"ok": False, "error": "missing_active_session", "message": "No active Antigravity interactive session exists."}
        session = self.interactive_session_status(target_session_id)
        if not session.get("active"):
            return {"ok": False, "error": "inactive_session", "message": "The Antigravity interactive session is no longer active.", "session": session}
        updated = self.session_store.append_interactive_input(target_session_id, text)
        return {"ok": True, "message": "Input queued for the active Antigravity interactive session.", "session": updated}

    def _registry_models(self, *, force_refresh: bool = False) -> list[str]:
        try:
            return [str(item).strip() for item in self.registry.get_models(force_refresh=force_refresh) if str(item).strip()]
        except Exception:
            return []

    @staticmethod
    def _probe_inventory_kind(probe: dict[str, Any]) -> str:
        return str(probe.get("probe_kind") or "inventory")

    @classmethod
    def _probe_has_inventory(cls, probe: dict[str, Any]) -> bool:
        return cls._probe_inventory_kind(probe) == "inventory"

    @classmethod
    def _probe_models(cls, probe: dict[str, Any]) -> list[str]:
        models = [str(item).strip() for item in probe.get("models", []) if str(item).strip()]
        if models:
            return models
        if not probe.get("ok") or not cls._probe_has_inventory(probe):
            return []
        stdout = str(probe.get("stdout") or "")
        return [line.strip() for line in stdout.splitlines() if line.strip()]

    def verify_auth(self) -> dict[str, Any]:
        verify = self.probe_api_key_models()
        verify["action"] = "verify"
        return self._annotate_verify_result(verify)

    def _api_mode(self) -> str:
        return "api_key"

    def ensure_authorized(self) -> dict[str, Any]:
        verify = self.verify_auth()
        if verify.get("ok"):
            models = list(verify.get("models") or [])
            self.session_store.record_success(models=models, auth_mode=str(verify.get("auth_mode") or self._api_mode()))
            verify["action"] = "verify"
            return verify
        self.session_store.record_failure(verify.get("stderr") or verify.get("error") or "antigravity_not_ready", failure_kind=str(verify.get("failure_kind") or "unknown"))
        verify["action"] = "verify"
        verify["auto_login_skipped"] = True
        verify["login_suppressed"] = True
        verify["suppression_reason"] = "api_token_only"
        verify["auth_mode"] = str(verify.get("auth_mode") or self._api_mode())
        return verify

    def probe_api_key_models(self) -> dict[str, Any]:
        if not self.api_key:
            return {"ok": False, "stdout": "", "stderr": "missing_api_key", "error": "missing_api_key", "status_code": None, "models": [], "auth_mode": self._api_mode(), "probe_kind": "inventory", "inventory_source": "api_key"}
        try:
            response = httpx.get(self.models_endpoint, headers=antigravity_request_headers(self.api_key), timeout=self.probe_timeout)
            payload = response.json() if response.content else {}
            models = self._models_from_payload(payload)
            stdout = "\n".join(models)
            ok = response.status_code == 200
            error = None if ok else response.text[:500]
            return {
                "ok": ok,
                "status_code": response.status_code,
                "stdout": stdout,
                "stderr": "" if ok else error,
                "error": error,
                "models": models,
                "auth_mode": self._api_mode(),
                "probe_kind": "inventory",
                "inventory_source": "proxy" if self.proxy_url else "api_key",
            }
        except Exception as exc:
            return {"ok": False, "status_code": None, "stdout": "", "stderr": str(exc), "error": str(exc), "models": [], "auth_mode": self._api_mode(), "probe_kind": "inventory", "inventory_source": "api_key"}

    def list_models(self) -> list[str]:
        res = self.probe_api_key_models()
        models = self._probe_models(res)
        if models:
            return models
        registry_models = self._registry_models(force_refresh=False)
        if registry_models:
            return registry_models
        return []

    def _generation_probe(self, models: list[str]) -> dict[str, Any]:
        ordered: list[str] = []
        preferred_candidates = [
            self.default_model,
            "antigravity-flash",
            "antigravity-flash-lite",
            *(models or []),
        ]
        seen: set[str] = set()
        for candidate in preferred_candidates:
            resolved = resolve_antigravity_model_alias(candidate, models)
            if not resolved or resolved in seen:
                continue
            seen.add(resolved)
            ordered.append(resolved)
        last = {"ok": False, "stdout": "", "stderr": "no_models_available", "error": "no_models_available", "status_code": None, "auth_mode": "api_key"}
        for candidate in ordered:
            probe = self._probe_generation(candidate)
            if probe.get("ok"):
                return probe
            last = probe
        return last

    def status(self) -> dict[str, Any]:
        models_res = self.probe_api_key_models()
        models = self._probe_models(models_res)
        inventory_probe_kind = self._probe_inventory_kind(models_res)
        inventory_source = str(models_res.get("inventory_source") or ("proxy" if self.proxy_url else "api_key"))
        api_res: dict[str, Any] | None = models_res
        auth_res: dict[str, Any] | None = models_res
        generation_probe = self._generation_probe(models)

        if not models:
            registry_models = self._registry_models(force_refresh=False)
            if registry_models:
                models = registry_models
                inventory_source = "registry"

        ready = bool(models_res.get("ok") and generation_probe.get("ok"))
        if not ready and not models and api_res and api_res.get("ok"):
            ready = True

        failure_text = " ".join(
            str(part)
            for part in [
                (models_res or {}).get("stderr"),
                (models_res or {}).get("error"),
                (generation_probe or {}).get("stderr"),
                (generation_probe or {}).get("error"),
            ]
            if str(part or "").strip()
        ).strip()
        failure_kind = self._classify_failure_text(failure_text)

        if not ready and failure_kind == "unknown" and not self.api_key:
            failure_kind = "missing_api_key"

        return {
            "ready": ready,
            "models": models,
            "models_probe": models_res,
            "generation_probe": generation_probe,
            "auth_probe": auth_res,
            "api_probe": api_res,
            "auth_mode": str(api_res.get("auth_mode") or self._api_mode()) if isinstance(api_res, dict) else self._api_mode(),
            "inventory_ok": bool(models),
            "inventory_source": inventory_source,
            "inventory_probe_kind": inventory_probe_kind,
            "failure_kind": failure_kind if not ready else "",
        }

    def is_ready(self) -> bool:
        return self.status().get("ready") is True

    def session_control_status(self) -> dict[str, Any]:
        status = self.status()
        auth_probe = dict(status.get("auth_probe") or {})
        store = self.session_store.snapshot()
        active_session = self.interactive_session_status()
        active = bool(active_session.get("active"))
        failure_kind = str(auth_probe.get("failure_kind") or "")
        ready = bool(status.get("ready"))
        login_suppressed = bool(auth_probe.get("login_suppressed"))
        suppression_reason = str(auth_probe.get("suppression_reason") or "")

        if ready:
            session_state = "ready"
            user_action_required = False
            message_for_user = "Antigravity API token flow is active. No login action is required."
            message_for_orchestrator = "Continue using Antigravity normally and refresh status on schedule."
        elif active and str(active_session.get("state") or "") == "waiting_code":
            session_state = "waiting_code"
            user_action_required = True
            message_for_user = "Antigravity is waiting for the authorization code. Submit it through the bridge session instead of restarting login."
            message_for_orchestrator = "Keep the existing managed login session alive and wait for user input through the bridge."
        elif active and str(active_session.get("state") or "") in {"waiting_browser", "login_pending", "running", "starting", "pending_verification"}:
            session_state = "login_pending"
            user_action_required = False
            message_for_user = "Antigravity login is already running. Finish the current browser flow and wait for session verification."
            message_for_orchestrator = "Do not reopen the browser. Preserve the active managed login session until it resolves."
        elif login_suppressed and suppression_reason == "interactive_login_recently_started":
            session_state = "login_pending"
            user_action_required = False
            message_for_user = "Antigravity authorization was already opened recently. Finish the existing browser login instead of starting a new one."
            message_for_orchestrator = "Do not open the browser again yet. Wait for the existing interactive login window to complete or expire."
        elif login_suppressed and suppression_reason == "cached_session_recently_verified":
            session_state = "degraded_transient"
            user_action_required = False
            message_for_user = "Antigravity session was recently valid. Temporary network or probe failure detected; no relogin needed now."
            message_for_orchestrator = "Keep the session, suppress relogin spam, and retry health checks later."
        elif login_suppressed and suppression_reason == "login_failure_cooldown":
            session_state = "cooldown"
            user_action_required = False
            message_for_user = "Recent login attempt already failed. The system is waiting before asking again."
            message_for_orchestrator = "Honor cooldown and avoid repeated login prompts until the cooldown expires."
        elif login_suppressed and suppression_reason == "api_token_only":
            session_state = "api_token_only"
            user_action_required = True
            message_for_user = "Provide a valid Antigravity API token or fix the HTTP/HTTPS or ws/wss API endpoint."
            message_for_orchestrator = "Request token-based remediation only."
        elif failure_kind in {"auth_required", "missing_api_key"}:
            session_state = "auth_required"
            user_action_required = True
            message_for_user = "Antigravity API token is missing or rejected. Provide a valid HTTP/HTTPS or ws/wss token for the configured endpoint."
            message_for_orchestrator = "Request token remediation only."
        elif failure_kind == "cli_missing":
            session_state = "cli_missing"
            user_action_required = True
            message_for_user = "Antigravity token-based API endpoint is required."
            message_for_orchestrator = "Remediate the token-based API endpoint instead."
        elif failure_kind == "unsupported_client":
            session_state = "legacy_cli_unsupported"
            user_action_required = True
            message_for_user = "The legacy Gemini CLI path is disabled; use the Antigravity API token flow."
            message_for_orchestrator = "Use the Antigravity API token endpoint or disable this provider until the endpoint is installed."
        else:
            session_state = "degraded_unknown"
            user_action_required = False
            message_for_user = "Antigravity is not ready, but the failure is not clearly an auth problem yet."
            message_for_orchestrator = "Recheck health and classify the failure before requesting another login."

        return {
            "controller": "AntigravityManager",
            "runtime_owner": "orchestrator",
            "user_action_required": user_action_required,
            "session_state": session_state,
            "auth_mode": str(status.get("auth_mode") or store.get("auth_mode") or self._api_mode()),
            "last_success_at": store.get("last_success_at", ""),
            "last_login_failure_at": store.get("last_login_failure_at", ""),
            "last_login_started_at": store.get("last_login_started_at", ""),
            "last_browser_opened_at": store.get("last_browser_opened_at", ""),
            "last_browser_url": store.get("last_browser_url", ""),
            "session_age_sec": self.session_store.success_age_sec(),
            "login_started_age_sec": self.session_store.login_started_age_sec(),
            "browser_open_age_sec": self.session_store.browser_open_age_sec(),
            "login_failure_age_sec": self.session_store.login_failure_age_sec(),
            "interactive_session_age_sec": self.session_store.interactive_session_age_sec(),
            "login_suppressed": login_suppressed,
            "suppression_reason": suppression_reason,
            "message_for_user": message_for_user,
            "message_for_orchestrator": message_for_orchestrator,
            "interactive_session": {
                "session_id": active_session.get("session_id", ""),
                "state": active_session.get("state", "idle"),
                "owner": active_session.get("owner", ""),
                "control_mode": active_session.get("control_mode", "bridge"),
                "message": active_session.get("message", ""),
                "browser_url": active_session.get("browser_url", ""),
                "last_prompt": active_session.get("last_prompt", ""),
                "input_hint": active_session.get("input_hint", ""),
                "user_input_required": bool(active_session.get("user_input_required")),
                "transcript_path": active_session.get("transcript_path", ""),
                "active": active,
            },
            "responsibility": {
                "interactive_login": "user" if session_state in {"auth_required", "waiting_code", "api_token_only"} else "AntigravityManager",
                "session_validation": "AntigravityManager",
                "runtime_watchdog": "AntigravityStatusModule",
                "relogin_policy": "AntigravityManager",
            },
        }

    def _api_probe_for_models(self, models: list[str]) -> dict[str, Any]:
        return self._probe_generation(models[0] if models else None)

    def _run_agy(self, args: list[str], *, timeout: int | None = None) -> dict[str, Any]:
        if not args or args == ["models"]:
            return self.probe_api_key_models()
        if args[:1] == ["-p"]:
            prompt = args[1] if len(args) > 1 else ""
            model = args[3] if len(args) > 3 and args[2] == "--model" else None
            probe = self._probe_generation(model)
            probe["prompt"] = prompt
            return probe
        return {"ok": False, "stdout": "", "stderr": "antigravity_api_only", "error": "antigravity_api_only", "command": ["antigravity-api", *args]}
