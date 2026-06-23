from __future__ import annotations

import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

try:
    from tenacity import RetryError, Retrying, retry_if_exception_type, stop_after_attempt, wait_exponential_jitter
except Exception:  # pragma: no cover
    RetryError = None  # type: ignore
    Retrying = None  # type: ignore
    retry_if_exception_type = None  # type: ignore
    stop_after_attempt = None  # type: ignore
    wait_exponential_jitter = None  # type: ignore

from core.core.gemini_runtime_router import AntigravityRuntimeRouter
from core.core.host_bridge import HostBridge
from core.core.models import Task
from core.core.provider_credentials import sync_provider_env_aliases


@dataclass(slots=True)
class BridgeExecResult:
    ok: bool
    output: str
    error: str
    provider: str
    model: str
    attempts: int
    error_type: str = "unknown"


class ExternalAIBridge:
    def __init__(self, host_bridge: HostBridge | None = None) -> None:
        self.host_bridge = host_bridge
        self.router = AntigravityRuntimeRouter()
        self.proxy_url = self._normalize_base_url(os.getenv("AI_BRIDGE_ANTIGRAVITY_PROXY_URL", ""))
        self.api_base_url = self._normalize_base_url(
            os.getenv(
                "AI_BRIDGE_ANTIGRAVITY_API_BASE_URL",
                os.getenv("GEMINI_API_BASE_URL", "https://generativelanguage.googleapis.com/v1beta"),
            )
        )
        self.api_key = (
            os.getenv("ANTIGRAVITY_API_KEY")
            or os.getenv("GEMINI_API_KEY")
            or os.getenv("GOOGLE_API_KEY")
            or ""
        ).strip()

    @staticmethod
    def resolve_antigravity_cli_command() -> list[str] | None:
        return None

    @staticmethod
    def _prefer_oauth_cli() -> bool:
        return os.getenv("AI_BRIDGE_ANTIGRAVITY_PREFER_OAUTH", "false").strip().lower() in {"1", "true", "yes", "on"}

    @staticmethod
    def _normalize_base_url(url: str) -> str:
        raw = str(url or "").strip().rstrip("/")
        if not raw:
            return ""
        if raw.startswith("ws://"):
            return "http://" + raw[5:]
        if raw.startswith("wss://"):
            return "https://" + raw[6:]
        return raw

    def _endpoint_base_url(self) -> str:
        return self.proxy_url or self.api_base_url

    @staticmethod
    def _antigravity_runtime_env(command_name: str | None = None) -> dict[str, str]:
        env = dict(sync_provider_env_aliases(os.environ.copy()))
        home_dir = env.get("HOME", "")
        extra_bins: list[str] = []
        if home_dir:
            extra_bins.extend([
                os.path.join(home_dir, ".npm-packages", "bin"),
                os.path.join(home_dir, ".local", "bin"),
            ])
        var_home = Path("/var/home")
        if var_home.is_dir():
            for user_home in var_home.glob("*"):
                if user_home.is_dir():
                    extra_bins.extend([
                        str(user_home / ".npm-packages" / "bin"),
                        str(user_home / ".local" / "bin"),
                    ])
        current_path = env.get("PATH", "")
        parts = [part for part in current_path.split(os.pathsep) if part]
        merged: list[str] = []
        for part in [*extra_bins, *parts]:
            if part and part not in merged:
                merged.append(part)
        env["PATH"] = os.pathsep.join(merged)
        normalized_command = (command_name or "").strip().lower()
        if ExternalAIBridge._prefer_oauth_cli() or (normalized_command and normalized_command in {"agy", "antigravity", "gemini"}):
            for key in ("ANTIGRAVITY_API_KEY", "GEMINI_API_KEY", "GOOGLE_API_KEY"):
                env.pop(key, None)
        return env

    @staticmethod
    def _timeout_error_detail(exc: Exception) -> str:
        return str(exc)

    def _request_json(self, method: str, path: str, *, timeout_sec: float, json_body: dict[str, Any] | None = None, params: dict[str, Any] | None = None) -> httpx.Response:
        base_url = self._endpoint_base_url()
        if not base_url:
            raise RuntimeError("antigravity_api_base_url_missing")
        headers: dict[str, str] = {}
        query: dict[str, Any] = dict(params or {})
        if self.api_key:
            if "generativelanguage.googleapis.com" in base_url:
                query.setdefault("key", self.api_key)
            else:
                headers.setdefault("Authorization", f"Bearer {self.api_key}")
        url = f"{base_url.rstrip('/')}/{path.lstrip('/')}"
        return httpx.request(method, url, params=query or None, json=json_body, headers=headers or None, timeout=timeout_sec)

    def _run_prompt_via_proxy(self, prompt: str, timeout_sec: int) -> BridgeExecResult | None:
        if not self.proxy_url:
            return None
        try:
            response = self._request_json("POST", "prompt", timeout_sec=timeout_sec + 10, json_body={"prompt": prompt, "timeout_sec": timeout_sec})
            payload = response.json() if response.content else {}
            output = str(payload.get("stdout") or self._extract_text(payload)).strip()
            err = str(payload.get("stderr") or payload.get("error") or "").strip()
            ok = bool(payload.get("ok") if isinstance(payload, dict) else response.status_code == 200)
            if ok and output:
                return BridgeExecResult(True, output, "", "antigravity", "antigravity-proxy", 1, error_type="none")
            error = err or response.text[:500] or "proxy_error"
            return BridgeExecResult(False, "", error, "antigravity", "antigravity-proxy", 1, error_type=self.classify_error(error))
        except Exception as exc:
            err = f"proxy_error: {exc}"
            return BridgeExecResult(False, "", err, "antigravity", "antigravity-proxy", 1, error_type=self.classify_error(err))

    @staticmethod
    def _retries() -> int:
        raw = os.getenv("EXTERNAL_AI_RETRIES", "3").strip()
        try:
            return max(1, int(raw))
        except ValueError:
            return 3

    @staticmethod
    def _backoff_sec(attempt: int) -> float:
        return min(8.0, 1.25 * attempt)

    @staticmethod
    def _is_capacity_error(stderr: str) -> bool:
        text = (stderr or "").lower()
        return "resource_exhausted" in text or "model_capacity_exhausted" in text or "status 429" in text

    @staticmethod
    def _is_token_error(stderr: str) -> bool:
        text = (stderr or "").lower()
        token_markers = ["token", "context length", "max output tokens", "quota exceeded"]
        return any(marker in text for marker in token_markers)

    @staticmethod
    def _estimate_consumed_tokens(prompt: str, output: str) -> int:
        return max(8, (len(prompt) + len(output)) // 4)

    @staticmethod
    def antigravity_auth_diagnostics() -> dict[str, object]:
        home = os.getenv("HOME", "")
        app_dir = os.path.join(home, ".antigravity", "antigravity-cli") if home else ""
        settings = os.path.join(app_dir, "settings.json") if app_dir else ""
        return {
            "app_data_dir_present": bool(app_dir and os.path.isdir(app_dir)),
            "settings_present": bool(settings and os.path.isfile(settings)),
            "live_probe_required": True,
            "note": "Antigravity uses token-based HTTP/HTTPS or ws/wss API access; live API probe is authoritative.",
        }

    @staticmethod
    def _extract_text(payload: Any) -> str:
        if not isinstance(payload, dict):
            return ""
        for key in ("stdout", "text", "output", "response"):
            value = str(payload.get(key) or "").strip()
            if value:
                return value
        candidates = payload.get("candidates")
        if isinstance(candidates, list):
            for candidate in candidates:
                if not isinstance(candidate, dict):
                    continue
                content = candidate.get("content")
                if not isinstance(content, dict):
                    continue
                parts = content.get("parts")
                if not isinstance(parts, list):
                    continue
                chunks: list[str] = []
                for part in parts:
                    if isinstance(part, dict):
                        text = str(part.get("text") or "").strip()
                        if text:
                            chunks.append(text)
                if chunks:
                    return "\n".join(chunks).strip()
        return ""

    @staticmethod
    def classify_error(raw_error: str, task: Task | None = None, api: Any | None = None, model: str = "unknown") -> str:
        text = (raw_error or "").lower()

        if api and task:
            intel = api.get_module("intelligence")
            if intel:
                diagnosis = intel.diagnose_error(raw_error, task, model)
                if diagnosis:
                    return diagnosis.error_type

        if "resource_exhausted" in text or "quota" in text or "429" in text:
            return "quota_exhaustion"
        if any(marker in text for marker in ["401", "403", "api key", "auth", "unauthorized", "forbidden"]):
            return "auth_fail"
        if any(marker in text for marker in ["unsupported_client", "ineligibletiererror", "migrate to the antigravity suite of products"]):
            return "unsupported_client"
        if any(marker in text for marker in ["connecttimeout", "readtimeout", "timed out", "connection timed out", "tcp"]):
            return "tcp_timeout"
        if any(marker in text for marker in [
            "deadline exceeded",
            "request timeout",
            "504",
            "502",
            "bad gateway",
            "gateway timeout",
            "api timeout",
            "service temporarily unavailable",
            "temporarily unavailable",
            "stream disconnected before completion",
            "connection closed before response completed",
            "server disconnected",
            "upstream connect error",
        ]):
            return "api_timeout"
        if any(marker in text for marker in ["hang", "stuck", "did not finish", "no response"]):
            return "sdk_hang"
        return "unknown"

    def _run_prompt_via_api(self, model: str, prompt: str, timeout_sec: int) -> BridgeExecResult:
        base_url = self._endpoint_base_url()
        if not base_url:
            return BridgeExecResult(False, "", "antigravity_api_base_url_missing", "antigravity", model, 0, error_type="unknown")
        if not self.api_key:
            return BridgeExecResult(False, "", "missing_api_key", "antigravity", model, 0, error_type="auth_fail")
        try:
            if self.proxy_url:
                response = self._request_json("POST", "prompt", timeout_sec=timeout_sec + 10, json_body={"prompt": prompt, "timeout_sec": timeout_sec})
            else:
                response = self._request_json(
                    "POST",
                    f"models/{model}:generateContent",
                    timeout_sec=timeout_sec + 10,
                    json_body={"contents": [{"role": "user", "parts": [{"text": prompt}]}]},
                )
            payload = response.json() if response.content else {}
            output = self._extract_text(payload).strip()
            err = str(payload.get("stderr") or payload.get("error") or "").strip() if isinstance(payload, dict) else ""
            if response.status_code == 200 and output and not self._response_output_error(output, err):
                return BridgeExecResult(True, output, "", "antigravity", model, 1, error_type="none")
            error = err or response.text[:500] or "antigravity_api_error"
            output_error = self._response_output_error(output, error)
            if output_error:
                error = output_error
            return BridgeExecResult(False, "", error, "antigravity", model, 1, error_type=self.classify_error(error))
        except Exception as exc:
            err = f"execution_error: {exc}"
            return BridgeExecResult(False, "", err, "antigravity", model, 1, error_type=self.classify_error(err))

    @staticmethod
    def _response_output_error(stdout: str, stderr: str = "") -> str:
        combined = f"{stdout or ''}\n{stderr or ''}".strip()
        text = combined.lower()
        markers = [
            "authentication required",
            "authentication timed out",
            "paste the authorization code",
            "please sign in",
            "error: authentication",
            "error: please sign in",
        ]
        return combined if any(marker in text for marker in markers) else ""

    def run_antigravity_cli(self, task: Task, prompt: str, timeout_sec: int = 120) -> BridgeExecResult:
        proxied = self._run_prompt_via_proxy(prompt, timeout_sec)
        if proxied is not None:
            return proxied
        retries = self._retries()
        plan = self.router.build_plan(task, prompt)
        attempts = 0
        last_error = ""

        for model in plan.models:
            if Retrying is not None:
                try:
                    retry_loop = Retrying(
                        stop=stop_after_attempt(retries),
                        wait=wait_exponential_jitter(initial=1, max=8),
                        retry=retry_if_exception_type((RuntimeError,)),
                        reraise=True,
                    )
                    for attempt in retry_loop:
                        with attempt:
                            attempts += 1
                            result = self._run_prompt_via_api(model, prompt, timeout_sec)
                            if result.ok:
                                getattr(self.router, "register_usage", lambda *args, **kwargs: None)(task, self._estimate_consumed_tokens(prompt, result.output))
                                return result
                            last_error = result.error or "unknown"
                            if self._is_capacity_error(last_error) or self._is_token_error(last_error) or self.classify_error(last_error) in {"quota_exhaustion", "auth_fail"}:
                                raise RuntimeError(last_error)
                            return result
                except RetryError as exc:
                    last_error = str(exc)
                except RuntimeError as exc:
                    last_error = str(exc)
                    if model and (self._is_capacity_error(last_error) or self._is_token_error(last_error) or self.classify_error(last_error) in {"quota_exhaustion", "auth_fail"}):
                        getattr(self.router, "block_model", lambda *args, **kwargs: None)(task, model)
                except Exception as exc:
                    return BridgeExecResult(False, "", f"execution_error: {exc}", "antigravity", model, attempts, error_type=self.classify_error(str(exc)))
                continue

            for attempt in range(1, retries + 1):
                attempts += 1
                result = self._run_prompt_via_api(model, prompt, timeout_sec)
                if result.ok:
                    getattr(self.router, "register_usage", lambda *args, **kwargs: None)(task, self._estimate_consumed_tokens(prompt, result.output))
                    return result

                last_error = result.error or "unknown"
                classified = result.error_type or self.classify_error(last_error)
                retryable = self._is_capacity_error(last_error) or self._is_token_error(last_error) or classified in {"quota_exhaustion", "auth_fail"}
                if retryable and attempt < retries:
                    time.sleep(self._backoff_sec(attempt))
                    continue
                if retryable:
                    getattr(self.router, "block_model", lambda *args, **kwargs: None)(task, model)
                    break
                return BridgeExecResult(False, "", last_error, "antigravity", model, attempts, error_type=classified)

        return BridgeExecResult(False, "", f"routing_exhausted: {last_error}", "antigravity", plan.models[-1], attempts, error_type=self.classify_error(last_error))

    def run_antigravity(self, task: Task, prompt: str, timeout_sec: int = 120) -> BridgeExecResult:
        return self.run_antigravity_cli(task, prompt, timeout_sec=timeout_sec)

    def run_gemini_cli(self, task: Task, prompt: str, timeout_sec: int = 120) -> BridgeExecResult:
        return self.run_antigravity(task, prompt, timeout_sec=timeout_sec)
