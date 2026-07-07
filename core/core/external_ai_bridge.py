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

from core.core.antigravity_provider import (
    antigravity_request_headers,
    extract_antigravity_response_text,
    resolve_antigravity_model_alias,
    resolve_antigravity_provider_config,
)
from core.core.antigravity_runtime_router import AntigravityRuntimeRouter
from core.core.mimo_provider import extract_mimo_response_text, invoke_mimo_native
from core.core.host_bridge import HostBridge
from core.core.models import Task
from core.core.openai_payload_guard import (
    EMPTY_ASSISTANT_RESPONSE_ERROR,
    EMPTY_PROVIDER_REQUEST_ERROR,
    has_meaningful_request_payload,
)
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
        cfg = resolve_antigravity_provider_config()
        self.proxy_url = self._normalize_base_url(os.getenv("AI_BRIDGE_ANTIGRAVITY_PROXY_URL", ""))
        self.api_base_url = self._normalize_base_url(os.getenv("AI_BRIDGE_ANTIGRAVITY_API_BASE_URL", cfg.base_url))
        self.chat_completions_endpoint = cfg.chat_completions_endpoint
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
        headers = antigravity_request_headers(self.api_key) if self.api_key else {}
        query: dict[str, Any] = dict(params or {})
        url = f"{base_url.rstrip('/')}/{path.lstrip('/')}"
        return httpx.request(method, url, params=query or None, json=json_body, headers=headers or None, timeout=timeout_sec)

    def _run_prompt_via_proxy(self, prompt: str, timeout_sec: int) -> BridgeExecResult | None:
        if not self.proxy_url:
            return None
        if not has_meaningful_request_payload(prompt):
            return BridgeExecResult(False, "", EMPTY_PROVIDER_REQUEST_ERROR, "antigravity", "antigravity-proxy", 0, error_type="empty_request")
        try:
            response = self._request_json("POST", "prompt", timeout_sec=timeout_sec + 10, json_body={"prompt": prompt, "timeout_sec": timeout_sec})
            payload = response.json() if response.content else {}
            output = str(payload.get("stdout") or self._extract_text(payload)).strip()
            err = str(payload.get("stderr") or payload.get("error") or "").strip()
            ok = bool(payload.get("ok") if isinstance(payload, dict) else response.status_code == 200)
            if ok and output:
                return BridgeExecResult(True, output, "", "antigravity", "antigravity-proxy", 1, error_type="none")
            error = err or (EMPTY_ASSISTANT_RESPONSE_ERROR if ok and response.status_code == 200 else response.text[:500] or "proxy_error")
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
        return extract_antigravity_response_text(payload)

    @staticmethod
    def classify_error(raw_error: str, task: Task | None = None, api: Any | None = None, model: str = "unknown") -> str:
        text = (raw_error or "").lower()

        if EMPTY_PROVIDER_REQUEST_ERROR.lower() in text:
            return "empty_request"
        if EMPTY_ASSISTANT_RESPONSE_ERROR.lower() in text:
            return "empty_response"

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
        if not has_meaningful_request_payload(prompt):
            return BridgeExecResult(False, "", EMPTY_PROVIDER_REQUEST_ERROR, "antigravity", model, 0, error_type="empty_request")
        try:
            resolved_model = resolve_antigravity_model_alias(model)
            if self.proxy_url:
                response = self._request_json("POST", "prompt", timeout_sec=timeout_sec + 10, json_body={"prompt": prompt, "timeout_sec": timeout_sec})
            else:
                response = httpx.post(
                    self.chat_completions_endpoint,
                    headers=antigravity_request_headers(self.api_key),
                    json={
                        "model": resolved_model,
                        "messages": [{"role": "user", "content": prompt}],
                        "max_completion_tokens": 1200,
                        "temperature": 0.2,
                        "stream": False,
                    },
                    timeout=timeout_sec + 10,
                )
            payload = response.json() if response.content else {}
            output = self._extract_text(payload).strip()
            err = str(payload.get("stderr") or payload.get("error") or "").strip() if isinstance(payload, dict) else ""
            if response.status_code == 200 and output and not self._response_output_error(output, err):
                return BridgeExecResult(True, output, "", "antigravity", resolved_model, 1, error_type="none")
            error = err or (EMPTY_ASSISTANT_RESPONSE_ERROR if response.status_code == 200 and not output else response.text[:500] or "antigravity_api_error")
            output_error = self._response_output_error(output, error)
            if output_error:
                error = output_error
            return BridgeExecResult(False, "", error, "antigravity", resolved_model, 1, error_type=self.classify_error(error))
        except Exception as exc:
            err = f"execution_error: {exc}"
            return BridgeExecResult(False, "", err, "antigravity", model, 1, error_type=self.classify_error(err))

    @staticmethod
    def _provider_fallback_enabled() -> bool:
        return os.getenv("AI_BRIDGE_ANTIGRAVITY_ENABLE_PROVIDER_FALLBACK", "true").strip().lower() in {"1", "true", "yes", "on"}

    @staticmethod
    def _fallback_triggered(error_type: str, raw_error: str) -> bool:
        normalized_type = str(error_type or "").strip().lower()
        text = str(raw_error or "").strip().lower()
        if normalized_type in {"quota_exhaustion", "api_timeout", "tcp_timeout"}:
            return True
        markers = [
            "user location is not supported",
            "failed_precondition",
            "resource_exhausted",
            "quota exceeded",
            "too many requests",
            "high demand",
            "temporarily unavailable",
            "unavailable",
            '"status": "unavailable"',
            '"code": 503',
            '"code": 429',
            '"code": 400',
        ]
        return any(marker in text for marker in markers)

    @staticmethod
    def _mimo_fallback_model() -> str:
        return str(os.getenv("AI_BRIDGE_MIMO_DEFAULT_MODEL", "xiaomi/mimo-v2.5-pro") or "xiaomi/mimo-v2.5-pro").strip()

    @staticmethod
    def _ai_kernel_fallback_base_url() -> str:
        return str(os.getenv("AI_KERNEL_BASE_URL", "http://127.0.0.1:8012/v1") or "http://127.0.0.1:8012/v1").strip().rstrip('/')

    @staticmethod
    def _ai_kernel_fallback_model() -> str:
        return str(os.getenv("AI_KERNEL_MODEL_ALIAS", "hauhaucs-qwen36-35b-a3b-aggressive:q4_k_m") or "hauhaucs-qwen36-35b-a3b-aggressive:q4_k_m").strip()

    @staticmethod
    def _ai_kernel_fallback_key() -> str:
        return str(os.getenv("AI_KERNEL_API_KEY", "local") or "local").strip()

    def _run_prompt_via_mimo_fallback(self, prompt: str, timeout_sec: int) -> BridgeExecResult:
        model = self._mimo_fallback_model()
        if not has_meaningful_request_payload(prompt):
            return BridgeExecResult(False, "", EMPTY_PROVIDER_REQUEST_ERROR, "mimo", model, 0, error_type="empty_request")
        payload, error_text, status_code = invoke_mimo_native(model, prompt, timeout_sec=float(timeout_sec))
        output = extract_mimo_response_text(payload) if payload else ""
        if output.strip():
            return BridgeExecResult(True, output.strip(), "", "mimo", model, 1, error_type="provider_fallback")
        error = error_text or (EMPTY_ASSISTANT_RESPONSE_ERROR if status_code == 200 else f"status_code={status_code}" if status_code is not None else "mimo_fallback_failed")
        return BridgeExecResult(False, "", error, "mimo", model, 1, error_type=self.classify_error(error))

    def _run_prompt_via_ai_kernel_fallback(self, prompt: str, timeout_sec: int) -> BridgeExecResult:
        model = self._ai_kernel_fallback_model()
        base_url = self._ai_kernel_fallback_base_url()
        if not has_meaningful_request_payload(prompt):
            return BridgeExecResult(False, "", EMPTY_PROVIDER_REQUEST_ERROR, "ai_kernel", model, 0, error_type="empty_request")
        try:
            response = httpx.post(
                f"{base_url}/chat/completions",
                headers={"Authorization": f"Bearer {self._ai_kernel_fallback_key()}"},
                json={
                    "model": model,
                    "messages": [{"role": "user", "content": prompt}],
                    "temperature": 0.2,
                    "stream": False,
                },
                timeout=timeout_sec + 10,
            )
            payload = response.json() if response.content else {}
            output = self._extract_text(payload).strip()
            if response.status_code == 200 and output:
                return BridgeExecResult(True, output, "", "ai_kernel", model, 1, error_type="provider_fallback")
            error = EMPTY_ASSISTANT_RESPONSE_ERROR if response.status_code == 200 and not output else response.text[:500] or f"ai_kernel_status_{response.status_code}"
            return BridgeExecResult(False, "", error, "ai_kernel", model, 1, error_type=self.classify_error(error))
        except Exception as exc:
            err = f"ai_kernel_fallback_error: {exc}"
            return BridgeExecResult(False, "", err, "ai_kernel", model, 1, error_type=self.classify_error(err))

    def _run_provider_fallbacks(self, prompt: str, timeout_sec: int, *, error_type: str, raw_error: str) -> BridgeExecResult | None:
        if not self._provider_fallback_enabled() or not self._fallback_triggered(error_type, raw_error):
            return None
        mimo_result = self._run_prompt_via_mimo_fallback(prompt, timeout_sec)
        if mimo_result.ok:
            return mimo_result
        ai_kernel_result = self._run_prompt_via_ai_kernel_fallback(prompt, timeout_sec)
        if ai_kernel_result.ok:
            return ai_kernel_result
        combined = raw_error
        if mimo_result.error:
            combined = f"{combined} | mimo_fallback={mimo_result.error}".strip(" |")
        if ai_kernel_result.error:
            combined = f"{combined} | ai_kernel_fallback={ai_kernel_result.error}".strip(" |")
        return BridgeExecResult(False, "", combined or "provider_fallback_exhausted", "antigravity", "provider-fallback", 1, error_type=error_type or "unknown")

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
        if not has_meaningful_request_payload(prompt):
            return BridgeExecResult(False, "", EMPTY_PROVIDER_REQUEST_ERROR, "antigravity", "request-guard", 0, error_type="empty_request")
        proxied = self._run_prompt_via_proxy(prompt, timeout_sec)
        if proxied is not None:
            if proxied.ok:
                return proxied
            fallback = self._run_provider_fallbacks(prompt, timeout_sec, error_type=proxied.error_type, raw_error=proxied.error)
            return fallback or proxied
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
                            fallback = self._run_provider_fallbacks(prompt, timeout_sec, error_type=result.error_type, raw_error=last_error)
                            if fallback is not None:
                                return fallback
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
                fallback = self._run_provider_fallbacks(prompt, timeout_sec, error_type=classified, raw_error=last_error)
                if fallback is not None:
                    return fallback
                return BridgeExecResult(False, "", last_error, "antigravity", model, attempts, error_type=classified)

        final_error = f"routing_exhausted: {last_error}"
        fallback = self._run_provider_fallbacks(prompt, timeout_sec, error_type=self.classify_error(last_error), raw_error=final_error)
        if fallback is not None:
            return fallback
        return BridgeExecResult(False, "", final_error, "antigravity", plan.models[-1], attempts, error_type=self.classify_error(last_error))

    def run_antigravity(self, task: Task, prompt: str, timeout_sec: int = 120) -> BridgeExecResult:
        return self.run_antigravity_cli(task, prompt, timeout_sec=timeout_sec)

    def run_gemini_cli(self, task: Task, prompt: str, timeout_sec: int = 120) -> BridgeExecResult:
        return self.run_antigravity(task, prompt, timeout_sec=timeout_sec)
