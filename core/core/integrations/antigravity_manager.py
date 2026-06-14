from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any

import httpx

from core.core.env_loader import load_env_file
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
        self.api_key = (os.getenv("ANTIGRAVITY_API_KEY") or os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY") or "").strip()
        self.api_base_url = os.getenv("GEMINI_API_BASE_URL", "https://generativelanguage.googleapis.com/v1beta").rstrip("/")
        self.proxy_url = os.getenv("AI_BRIDGE_ANTIGRAVITY_PROXY_URL", "").strip().rstrip("/")
        self.session_store = AntigravitySessionStore()

    @staticmethod
    def _read_int(key: str, default: int) -> int:
        raw = os.getenv(key, str(default)).strip()
        try:
            return max(5, int(raw))
        except ValueError:
            return default

    @staticmethod
    def auto_login_enabled() -> bool:
        return os.getenv("AI_BRIDGE_ANTIGRAVITY_AUTO_LOGIN", "true").strip().lower() in {"1", "true", "yes", "on"}

    @staticmethod
    def _login_failure_cooldown_sec() -> int:
        raw = os.getenv("AI_BRIDGE_ANTIGRAVITY_LOGIN_FAILURE_COOLDOWN_SEC", "900").strip()
        try:
            return max(60, int(raw))
        except ValueError:
            return 900

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
        if "not found" in text or "no such file" in text:
            return "cli_missing"
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

    def _maybe_suppress_login(self, verify: dict[str, Any]) -> dict[str, Any] | None:
        failure_kind = str(verify.get("failure_kind") or "unknown")
        if self.session_store.login_failure_cooldown_active(cooldown_sec=self._login_failure_cooldown_sec()):
            verify["login_suppressed"] = True
            verify["suppression_reason"] = "login_failure_cooldown"
            return verify
        if failure_kind in {"transient", "unknown"} and bool(verify.get("auth_marker_present")) and self.session_store.recently_verified(within_sec=self._recent_session_grace_sec()):
            verify["login_suppressed"] = True
            verify["suppression_reason"] = "cached_session_recently_verified"
            return verify
        return None

    def _run_host(self, cmd: list[str], *, timeout: int | None = None) -> dict[str, Any]:
        try:
            result = self.host_bridge.execute(cmd, timeout=timeout or self.probe_timeout, check=False)
            return {
                "ok": result.returncode == 0,
                "stdout": result.stdout or "",
                "stderr": result.stderr or "",
                "exit_code": result.returncode,
                "command": cmd,
            }
        except Exception as exc:
            return {"ok": False, "stdout": "", "stderr": str(exc), "error": str(exc), "command": cmd}

    def _run_agy(self, args: list[str], *, timeout: int | None = None) -> dict[str, Any]:
        if self.proxy_url:
            return self._run_agy_via_proxy(args, timeout=timeout)
        return self._run_host(["agy", *args], timeout=timeout)

    def _run_agy_via_proxy(self, args: list[str], *, timeout: int | None = None) -> dict[str, Any]:
        if not args:
            return {"ok": False, "stdout": "", "stderr": "empty_args", "error": "empty_args", "command": ["agy"]}
        try:
            timeout_sec = timeout or self.probe_timeout
            if args[:1] == ["models"]:
                response = httpx.get(f"{self.proxy_url}/models", timeout=timeout_sec)
                payload = response.json()
                models = payload.get("models", []) if isinstance(payload, dict) else []
                stdout = "\n".join(str(item) for item in models)
                return {
                    "ok": bool(payload.get("ok")),
                    "stdout": stdout,
                    "stderr": str(payload.get("stderr", "")),
                    "exit_code": int(payload.get("exit_code", 0 if payload.get("ok") else 1)),
                    "command": ["agy", *args],
                }
            if args and args[0] == "-p":
                prompt = args[1] if len(args) > 1 else ""
                response = httpx.post(f"{self.proxy_url}/prompt", json={"prompt": prompt, "timeout_sec": timeout_sec}, timeout=timeout_sec + 10)
                payload = response.json()
                return {
                    "ok": bool(payload.get("ok")),
                    "stdout": str(payload.get("stdout", "")),
                    "stderr": str(payload.get("stderr", "")),
                    "exit_code": int(payload.get("exit_code", 0 if payload.get("ok") else 1)),
                    "command": ["agy", *args],
                }
            return {"ok": False, "stdout": "", "stderr": "unsupported_proxy_args", "error": "unsupported_proxy_args", "command": ["agy", *args]}
        except Exception as exc:
            return {"ok": False, "stdout": "", "stderr": str(exc), "error": str(exc), "command": ["agy", *args]}

    def _run_login_helper(self, args: list[str], *, timeout: int | None = None) -> dict[str, Any]:
        helper = Path(__file__).resolve().parents[2] / "scripts" / "antigravity_login.py"
        result = self._run_host(["python3", str(helper), *args], timeout=timeout)
        if "--json" not in args:
            return result
        stdout = str(result.get("stdout") or "").strip()
        if not stdout:
            return result
        try:
            import json
            payload = json.loads(stdout)
        except Exception:
            return result
        if not isinstance(payload, dict):
            return result
        merged = dict(result)
        merged.update(payload)
        if "ok" not in payload:
            merged["ok"] = result.get("ok", False)
        return merged

    def verify_auth(self) -> dict[str, Any]:
        return self._annotate_verify_result(self._run_login_helper(["--verify", "--json"], timeout=max(self.probe_timeout, 45)))

    def _confirmed_ready(self) -> dict[str, Any]:
        verify = self.verify_auth()
        if verify.get("ok"):
            models = list(verify.get("models") or [])
            self.session_store.record_success(models=models, auth_mode=str(verify.get("auth_mode") or "agy_oauth"))
            verify["action"] = "verify"
            return verify

        models = self._run_agy(["models"], timeout=max(self.probe_timeout, 45))
        if models.get("ok"):
            probe = self._run_agy(["-p", "healthcheck: reply with ok", "--print-timeout", f"{self.probe_timeout}s"], timeout=max(self.probe_timeout, 45))
            if probe.get("ok"):
                model_list = [line.strip() for line in models.get("stdout", "").splitlines() if line.strip()]
                self.session_store.record_success(models=model_list, auth_mode="agy_oauth")
                return {
                    "ok": True,
                    "action": "verify_after_login",
                    "models": model_list,
                    "models_probe": models,
                    "generation_probe": probe,
                    "auth_probe": verify,
                    "api_probe": None,
                    "auth_mode": "agy_oauth",
                }

        self.session_store.record_failure(verify.get("stderr") or verify.get("error") or "antigravity_not_ready", failure_kind=str(verify.get("failure_kind") or "unknown"))
        return verify

    def ensure_authorized(self) -> dict[str, Any]:
        verify = self._confirmed_ready()
        if verify.get("ok"):
            return verify

        suppressed = self._maybe_suppress_login(verify)
        if suppressed is not None:
            suppressed["action"] = "verify"
            return suppressed

        if not self.auto_login_enabled():
            verify["action"] = "verify"
            verify["auto_login_skipped"] = True
            return verify

        last: dict[str, Any] = verify
        for attempt in range(1, 4):
            login = self._run_login_helper(["--login", "--timeout", str(self.login_timeout)], timeout=self.login_timeout + 20)
            login["action"] = "login"
            login["attempt"] = attempt
            if login.get("ok"):
                confirmation = self._confirmed_ready()
                if confirmation.get("ok"):
                    self.session_store.record_success(models=list(confirmation.get("models") or []), auth_mode=str(confirmation.get("auth_mode") or "agy_oauth"))
                    confirmation["action"] = "login_confirmed"
                    confirmation["attempt"] = attempt
                    return confirmation
                login["verify_error"] = confirmation.get("stderr") or confirmation.get("error") or "login did not produce a ready auth state"
                login["post_login_verify"] = confirmation
                self.session_store.record_login_failure(str(login.get("verify_error") or "login_not_confirmed"), failure_kind=str(confirmation.get("failure_kind") or "unknown"))
                last = login
            else:
                self.session_store.record_login_failure(str(login.get("stderr") or login.get("error") or "login_failed"), failure_kind="auth_required")
                last = login
            if attempt < 3:
                import time
                time.sleep(min(8.0, 1.5 * attempt))
        return last



    @staticmethod
    def _cli_missing(probe: dict[str, Any]) -> bool:
        raw = f"{probe.get('stderr', '')} {probe.get('error', '')}".lower()
        return "no such file or directory" in raw or "not found" in raw

    def probe_api_key_models(self) -> dict[str, Any]:
        if not self.api_key:
            return {"ok": False, "models": [], "error": "missing_api_key", "auth_mode": "api_key"}
        try:
            response = httpx.get(f"{self.api_base_url}/models", params={"key": self.api_key}, timeout=self.probe_timeout)
            models: list[str] = []
            if response.status_code == 200:
                payload = response.json()
                for item in payload.get("models", []):
                    name = str(item.get("name", "")).strip()
                    if name:
                        models.append(name.rsplit("/", 1)[-1])
            return {
                "ok": response.status_code == 200,
                "status_code": response.status_code,
                "models": models,
                "error": None if response.status_code == 200 else response.text[:500],
                "auth_mode": "api_key",
            }
        except Exception as exc:
            return {"ok": False, "status_code": None, "models": [], "error": str(exc), "auth_mode": "api_key"}

    def is_ready(self) -> bool:
        return self.status().get("ready") is True

    def session_control_status(self) -> dict[str, Any]:
        status = self.status()
        auth_probe = dict(status.get("auth_probe") or {})
        store = self.session_store.snapshot()
        failure_kind = str(auth_probe.get("failure_kind") or "")
        ready = bool(status.get("ready"))
        login_suppressed = bool(auth_probe.get("login_suppressed"))
        suppression_reason = str(auth_probe.get("suppression_reason") or "")

        if ready:
            session_state = "ready"
            user_action_required = False
            message_for_user = "Antigravity session is active. No login action is required."
            message_for_orchestrator = "Continue using Antigravity normally and refresh status on schedule."
        elif failure_kind == "auth_required":
            session_state = "auth_required"
            user_action_required = True
            message_for_user = "Antigravity login is required once to restore the session."
            message_for_orchestrator = "Request one interactive user login and avoid repeated auto-login loops."
        elif failure_kind == "cli_missing":
            session_state = "cli_missing"
            user_action_required = True
            message_for_user = "Antigravity CLI is missing or not executable on this machine."
            message_for_orchestrator = "Do not retry login. Report a runtime dependency problem instead."
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
            "auth_mode": str(status.get("auth_mode") or store.get("auth_mode") or "agy_oauth"),
            "last_success_at": store.get("last_success_at", ""),
            "last_login_failure_at": store.get("last_login_failure_at", ""),
            "session_age_sec": self.session_store.success_age_sec(),
            "login_failure_age_sec": self.session_store.login_failure_age_sec(),
            "login_suppressed": login_suppressed,
            "suppression_reason": suppression_reason,
            "message_for_user": message_for_user,
            "message_for_orchestrator": message_for_orchestrator,
            "responsibility": {
                "interactive_login": "user" if session_state == "auth_required" else "AntigravityManager",
                "session_validation": "AntigravityManager",
                "runtime_watchdog": "AntigravityStatusModule",
                "relogin_policy": "AntigravityManager",
            },
        }

    def list_models(self) -> list[str]:
        res = self._run_agy(["models"])
        if res.get("ok"):
            return [line.strip() for line in res.get("stdout", "").splitlines() if line.strip()]
        return []

    def status(self) -> dict[str, Any]:
        models_res = self._run_agy(["models"])
        models = [line.strip() for line in models_res.get("stdout", "").splitlines() if line.strip()] if models_res.get("ok") else []
        probe_res = {"ok": False, "skipped": True}
        auth_res: dict[str, Any] | None = None
        api_res: dict[str, Any] | None = None
        auth_mode = "agy_oauth"

        if models_res.get("ok"):
            probe_res = self._run_agy(["-p", "healthcheck: reply with ok", "--print-timeout", f"{self.probe_timeout}s"])
        else:
            api_res = self.probe_api_key_models()
            if api_res.get("ok"):
                models = list(api_res.get("models", []))
                auth_mode = "api_key"
            elif self._cli_missing(models_res):
                auth_res = {"ok": False, "skipped": True, "reason": "agy_cli_missing"}
            else:
                verify = self.verify_auth()
                suppressed = self._maybe_suppress_login(verify)
                auth_res = suppressed if suppressed is not None else self.ensure_authorized()
                if auth_res.get("ok"):
                    models_res = self._run_agy(["models"])
                    models = [line.strip() for line in models_res.get("stdout", "").splitlines() if line.strip()] if models_res.get("ok") else []
                    if models_res.get("ok"):
                        probe_res = self._run_agy(["-p", "healthcheck: reply with ok", "--print-timeout", f"{self.probe_timeout}s"])

        ready = bool((models_res.get("ok") and probe_res.get("ok")) or (api_res and api_res.get("ok")))
        return {
            "ready": ready,
            "models": models,
            "models_probe": models_res,
            "generation_probe": probe_res,
            "auth_probe": auth_res,
            "api_probe": api_res,
            "auth_mode": auth_mode,
        }
