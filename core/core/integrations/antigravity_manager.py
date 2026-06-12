from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any

import httpx

from core.core.env_loader import load_env_file
from core.core.host_bridge import HostBridge

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
        return self._run_host(["agy", *args], timeout=timeout)

    def _run_login_helper(self, args: list[str], *, timeout: int | None = None) -> dict[str, Any]:
        helper = Path(__file__).resolve().parents[2] / "scripts" / "antigravity_login.py"
        return self._run_host(["python3", str(helper), *args], timeout=timeout)

    def verify_auth(self) -> dict[str, Any]:
        return self._run_login_helper(["--verify"], timeout=max(self.probe_timeout, 45))

    def _confirmed_ready(self) -> dict[str, Any]:
        verify = self.verify_auth()
        if verify.get("ok"):
            verify["action"] = "verify"
            return verify

        models = self._run_agy(["models"], timeout=max(self.probe_timeout, 45))
        if models.get("ok"):
            probe = self._run_agy(["-p", "healthcheck: reply with ok", "--print-timeout", f"{self.probe_timeout}s"], timeout=max(self.probe_timeout, 45))
            if probe.get("ok"):
                return {
                    "ok": True,
                    "action": "verify_after_login",
                    "models": [line.strip() for line in models.get("stdout", "").splitlines() if line.strip()],
                    "models_probe": models,
                    "generation_probe": probe,
                    "auth_probe": verify,
                    "api_probe": None,
                    "auth_mode": "agy_oauth",
                }

        return verify

    def ensure_authorized(self) -> dict[str, Any]:
        verify = self._confirmed_ready()
        if verify.get("ok"):
            return verify

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
                    confirmation["action"] = "login_confirmed"
                    confirmation["attempt"] = attempt
                    return confirmation
                login["verify_error"] = confirmation.get("stderr") or confirmation.get("error") or "login did not produce a ready auth state"
                login["post_login_verify"] = confirmation
                last = login
            else:
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
                auth_res = self.ensure_authorized()
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
