from __future__ import annotations

import logging
import os
import subprocess
import time
from pathlib import Path
from typing import Any

import httpx

from core.core.env_loader import load_env_file
from core.core.host_bridge import HostBridge
from core.core.external_ai_bridge import ExternalAIBridge
from core.core.gemini_model_registry import AntigravityModelRegistry
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
        self.registry = AntigravityModelRegistry()

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
        if "not found" in text or "no such file" in text:
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
        local = self._run_local_cli(args, timeout=timeout)
        if local.get("ok") or not self._cli_missing(local):
            return local
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

    def _confirmed_ready(self) -> dict[str, Any]:
        verify = self.verify_auth()
        if verify.get("ok"):
            models = list(verify.get("models") or [])
            self.session_store.record_success(models=models, auth_mode=str(verify.get("auth_mode") or "agy_oauth"))
            active = self._active_interactive_session()
            if active is not None:
                self.session_store.finish_interactive_session(str(active.get("session_id") or ""), state="ready", message="Antigravity session confirmed and kept alive.")
            verify["action"] = "verify"
            return verify

        models = self._run_agy(["models"], timeout=max(self.probe_timeout, 45))
        if models.get("ok"):
            probe = self._run_agy(["-p", "healthcheck: reply with ok", "--print-timeout", f"{self.probe_timeout}s"], timeout=max(self.probe_timeout, 45))
            if probe.get("ok"):
                model_list = [line.strip() for line in models.get("stdout", "").splitlines() if line.strip()]
                self.session_store.record_success(models=model_list, auth_mode="agy_oauth")
                active = self._active_interactive_session()
                if active is not None:
                    self.session_store.finish_interactive_session(str(active.get("session_id") or ""), state="ready", message="Antigravity session confirmed and kept alive.")
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
            self.session_store.record_login_started()
            login = self._run_login_helper(["--managed-login-start", "--timeout", str(self.login_timeout), "--json"], timeout=self.login_timeout + 20)
            login["action"] = "managed_login_start"
            login["attempt"] = attempt
            session = dict(login.get("session") or {})
            if login.get("ok") and session.get("session_id"):
                return self._pending_session_response(verify, session, reason="interactive_session_active")
            if login.get("ok"):
                confirmation = self._confirmed_ready()
                if confirmation.get("ok"):
                    confirmation["action"] = "login_confirmed"
                    confirmation["attempt"] = attempt
                    return confirmation
                login["verify_error"] = confirmation.get("stderr") or confirmation.get("error") or "login did not produce a ready auth state"
                login["post_login_verify"] = confirmation
                self.session_store.record_login_failure(str(login.get("verify_error") or "login_not_confirmed"), failure_kind=str(confirmation.get("failure_kind") or "unknown"))
                last = login
            else:
                self.session_store.record_login_failure(str(login.get("stderr") or login.get("error") or login.get("message") or "login_failed"), failure_kind="auth_required")
                last = login
            last = login
            if attempt < 3:
                time.sleep(min(8.0, 1.5 * attempt))
        return last

    @staticmethod
    def _cli_missing(probe: dict[str, Any]) -> bool:
        raw = f"{probe.get('stderr', '')} {probe.get('error', '')}".lower()
        return any(marker in raw for marker in ["no such file or directory", "not found", "node: command not found", 'env: "node"', "antigravity_cli_not_found"])

    @staticmethod
    def _timeout_detail(exc: subprocess.TimeoutExpired) -> tuple[str, str]:
        stdout = exc.stdout.decode() if isinstance(exc.stdout, bytes) else str(exc.stdout or "")
        stderr = exc.stderr.decode() if isinstance(exc.stderr, bytes) else str(exc.stderr or "")
        detail = "\n".join(part.strip() for part in (stderr, stdout) if str(part).strip()).strip()
        return stdout, detail or f"timeout: {exc}"

    @staticmethod
    def _probe_inventory_kind(probe: dict[str, Any]) -> str:
        return str(probe.get("probe_kind") or "inventory")

    @classmethod
    def _probe_has_inventory(cls, probe: dict[str, Any]) -> bool:
        return cls._probe_inventory_kind(probe) == "inventory"

    @classmethod
    def _probe_models(cls, probe: dict[str, Any]) -> list[str]:
        if not probe.get("ok") or not cls._probe_has_inventory(probe):
            return []
        return [line.strip() for line in str(probe.get("stdout") or "").splitlines() if line.strip()]

    def _registry_models(self, *, force_refresh: bool = False) -> list[str]:
        try:
            return [str(item).strip() for item in self.registry.get_models(force_refresh=force_refresh) if str(item).strip()]
        except Exception:
            return []

    def _run_local_cli(self, args: list[str], *, timeout: int | None = None) -> dict[str, Any]:
        cmd_prefix = ExternalAIBridge.resolve_antigravity_cli_command()
        if not cmd_prefix:
            return {"ok": False, "stdout": "", "stderr": "antigravity_cli_not_found", "error": "antigravity_cli_not_found", "command": ["agy", *args]}

        cli_name = Path(cmd_prefix[0]).name.lower()
        env = ExternalAIBridge._antigravity_runtime_env(cli_name)
        repo_root = str(Path(__file__).resolve().parents[3])
        cmd = [*cmd_prefix, *args]

        structural_probe = False
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout or self.probe_timeout, env=env, cwd=repo_root)
            payload = {
                "ok": proc.returncode == 0,
                "stdout": proc.stdout or "",
                "stderr": proc.stderr or "",
                "exit_code": proc.returncode,
                "command": cmd,
            }
            if structural_probe:
                payload["probe_kind"] = "binary_presence"
                payload["binary_present"] = proc.returncode == 0
                payload["inventory_supported"] = False
            return payload
        except subprocess.TimeoutExpired as exc:
            stdout, detail = self._timeout_detail(exc)
            payload = {"ok": False, "stdout": stdout, "stderr": detail, "error": detail, "command": cmd, "timeout": True}
            if structural_probe:
                payload["probe_kind"] = "binary_presence"
                payload["binary_present"] = False
                payload["inventory_supported"] = False
            return payload
        except Exception as exc:
            payload = {"ok": False, "stdout": "", "stderr": str(exc), "error": str(exc), "command": cmd}
            if structural_probe:
                payload["probe_kind"] = "binary_presence"
                payload["binary_present"] = False
                payload["inventory_supported"] = False
            return payload

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
        active_session = self.interactive_session_status()
        active = bool(active_session.get("active"))
        failure_kind = str(auth_probe.get("failure_kind") or "")
        ready = bool(status.get("ready"))
        login_suppressed = bool(auth_probe.get("login_suppressed"))
        suppression_reason = str(auth_probe.get("suppression_reason") or "")

        if ready:
            session_state = "ready"
            user_action_required = False
            message_for_user = "Antigravity session is active. No login action is required."
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
        elif failure_kind == "unsupported_client":
            session_state = "legacy_cli_unsupported"
            user_action_required = True
            message_for_user = "Installed Gemini CLI is a legacy client that must be migrated to Antigravity-compatible runtime or API mode."
            message_for_orchestrator = "Do not retry OAuth/login loops. Migrate off the legacy Gemini CLI or disable this provider until a supported Antigravity runtime is installed."
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
                "interactive_login": "user" if session_state in {"auth_required", "waiting_code"} else "AntigravityManager",
                "session_validation": "AntigravityManager",
                "runtime_watchdog": "AntigravityStatusModule",
                "relogin_policy": "AntigravityManager",
            },
        }

    def list_models(self) -> list[str]:
        res = self._run_agy(["models"])
        models = self._probe_models(res)
        if models:
            return models
        registry_models = self._registry_models(force_refresh=False)
        if registry_models:
            return registry_models
        api_res = self.probe_api_key_models()
        if api_res.get("ok"):
            return [str(item).strip() for item in api_res.get("models", []) if str(item).strip()]
        return []

    def status(self) -> dict[str, Any]:
        models_res = self._run_agy(["models"])
        models = self._probe_models(models_res)
        probe_res = {"ok": False, "skipped": True}
        auth_res: dict[str, Any] | None = None
        api_res: dict[str, Any] | None = None
        auth_mode = "agy_oauth"
        inventory_probe_kind = self._probe_inventory_kind(models_res)
        inventory_source = "cli" if models else "unavailable"
        cli_failure_kind = self._classify_failure_text(f"{models_res.get('stderr', '')} {models_res.get('stdout', '')}")

        if models_res.get("ok"):
            if not models:
                registry_models = self._registry_models(force_refresh=False)
                if registry_models:
                    models = registry_models
                    inventory_source = "registry"
            probe_res = self._run_agy(["-p", "healthcheck: reply with ok", "--print-timeout", f"{self.probe_timeout}s"])
            if not probe_res.get("ok") and self.api_key:
                api_res = self.probe_api_key_models()
                if api_res.get("ok"):
                    auth_mode = str(api_res.get("auth_mode") or "api_key")
                    api_models = [str(item).strip() for item in api_res.get("models", []) if str(item).strip()]
                    if api_models:
                        models = api_models
                        inventory_source = "api_key"
        else:
            api_res = self.probe_api_key_models()
            if api_res:
                auth_mode = str(api_res.get("auth_mode") or "api_key")
            if api_res.get("ok"):
                models = [str(item).strip() for item in api_res.get("models", []) if str(item).strip()]
                if models:
                    inventory_source = "api_key"
            elif cli_failure_kind == "unsupported_client":
                auth_res = {
                    "ok": False,
                    "skipped": True,
                    "failure_kind": "unsupported_client",
                    "stderr": models_res.get("stderr") or models_res.get("stdout") or "legacy_gemini_cli_unsupported",
                }
                auth_mode = "legacy_gemini_cli"
            elif self._cli_missing(models_res):
                auth_res = {"ok": False, "skipped": True, "reason": "agy_cli_missing", "failure_kind": "cli_missing"}
            else:
                verify = self.verify_auth()
                suppressed = self._maybe_suppress_login(verify)
                auth_res = suppressed if suppressed is not None else self.ensure_authorized()
                if auth_res.get("ok"):
                    auth_mode = str(auth_res.get("auth_mode") or "agy_oauth")
                    models_res = self._run_agy(["models"])
                    inventory_probe_kind = self._probe_inventory_kind(models_res)
                    models = self._probe_models(models_res)
                    if models:
                        inventory_source = "cli"
                    else:
                        registry_models = self._registry_models(force_refresh=False)
                        if registry_models:
                            models = registry_models
                            inventory_source = "registry"
                    if models_res.get("ok"):
                        probe_res = self._run_agy(["-p", "healthcheck: reply with ok", "--print-timeout", f"{self.probe_timeout}s"])

        inventory_ok = bool(models)
        ready = bool((models_res.get("ok") and probe_res.get("ok")) or (api_res and api_res.get("ok")))
        return {
            "ready": ready,
            "models": models,
            "models_probe": models_res,
            "generation_probe": probe_res,
            "auth_probe": auth_res,
            "api_probe": api_res,
            "auth_mode": auth_mode,
            "inventory_ok": inventory_ok,
            "inventory_source": inventory_source,
            "inventory_probe_kind": inventory_probe_kind,
            "failure_kind": cli_failure_kind if not ready else "",
        }
