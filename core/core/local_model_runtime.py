from __future__ import annotations

import asyncio
import os
import time
from dataclasses import asdict, dataclass, field, replace
from typing import Any, Iterable, Mapping, Sequence

import httpx


DEFAULT_LOCAL_MODEL = "qwen2.5:32b-instruct-q4_k_m"
DEFAULT_LOCAL_ENDPOINT = "http://host.containers.internal:11434"
DEFAULT_GENERATION_SYSTEM = "You are a specialized AI Kernel Optimizer."


def _normalize_endpoint(endpoint: str) -> str:
    return endpoint.rstrip("/")


def _env_float(name: str, default: float, minimum: float) -> float:
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        return max(minimum, float(raw))
    except ValueError:
        return default


def _env_int(name: str, default: int, minimum: int) -> int:
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        return max(minimum, int(raw))
    except ValueError:
        return default


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return str(raw).strip().lower() in {"1", "true", "yes", "on"}


def _default_generation_options() -> dict[str, Any]:
    options: dict[str, Any] = {"temperature": 0.2, "top_p": 0.9}
    backend = str(
        os.getenv("AI_BRIDGE_LOCAL_LLM_GPU_BACKEND")
        or os.getenv("AI_BRIDGE_LOCAL_LLM_GPU_BACKEND_DETECTED")
        or ""
    ).strip().lower()
    gpu_enabled = _env_bool("OLLAMA_GPU_ENABLED", True)
    force_gpu = _env_bool("AI_BRIDGE_LOCAL_LLM_FORCE_GPU", True)
    if force_gpu and gpu_enabled and backend in {"nvidia", "amd", "intel"}:
        options["num_gpu"] = _env_int("AI_BRIDGE_LOCAL_LLM_NUM_GPU_LAYERS", 999, 1)
        main_gpu = os.getenv("AI_BRIDGE_LOCAL_LLM_MAIN_GPU")
        if main_gpu is not None:
            try:
                options["main_gpu"] = max(0, int(main_gpu))
            except ValueError:
                pass
    return options


def _candidate_endpoints(primary: str, extra: Sequence[str] = ()) -> tuple[str, ...]:
    values = [_normalize_endpoint(primary), *(_normalize_endpoint(item) for item in extra if item)]
    derived: list[str] = []
    for value in list(values):
        if "host.containers.internal" in value:
            derived.append(value.replace("host.containers.internal", "127.0.0.1"))
            derived.append(value.replace("host.containers.internal", "localhost"))
        elif "127.0.0.1" in value:
            derived.append(value.replace("127.0.0.1", "host.containers.internal"))
            derived.append(value.replace("127.0.0.1", "localhost"))
        elif "localhost" in value:
            derived.append(value.replace("localhost", "127.0.0.1"))
            derived.append(value.replace("localhost", "host.containers.internal"))
    ordered: list[str] = []
    for value in [*values, *derived]:
        if value and value not in ordered:
            ordered.append(value)
    return tuple(ordered)


@dataclass(slots=True)
class LocalModelRetryPolicy:
    max_attempts: int = 2
    backoff_base_sec: float = 0.2
    retry_status_codes: tuple[int, ...] = (408, 409, 425, 429, 500, 502, 503, 504)
    retry_transport_errors: bool = True

    def should_retry_status(self, status_code: int) -> bool:
        return status_code in self.retry_status_codes


@dataclass(slots=True)
class LocalModelRuntimeConfig:
    endpoint: str = DEFAULT_LOCAL_ENDPOINT
    fallback_endpoints: tuple[str, ...] = ()
    model_name: str = DEFAULT_LOCAL_MODEL
    health_timeout_sec: float = 1.0
    generation_timeout_sec: float = 60.0
    management_timeout_sec: float = 600.0
    default_system: str = DEFAULT_GENERATION_SYSTEM
    default_options: dict[str, Any] = field(default_factory=_default_generation_options)
    default_headers: dict[str, str] = field(default_factory=dict)
    retry_policy: LocalModelRetryPolicy = field(default_factory=LocalModelRetryPolicy)

    @classmethod
    def from_env(
        cls,
        endpoint: str | None = None,
        model_name: str | None = None,
        timeout_sec: float | None = None,
    ) -> LocalModelRuntimeConfig:
        base_endpoint = endpoint or os.getenv("AI_BRIDGE_LOCAL_LLM_ENDPOINT") or DEFAULT_LOCAL_ENDPOINT
        fallbacks_raw = os.getenv("AI_BRIDGE_LOCAL_LLM_FALLBACK_ENDPOINTS", "")
        fallback_endpoints = tuple(item.strip() for item in fallbacks_raw.split(",") if item.strip())
        health_timeout = max(0.2, timeout_sec if timeout_sec is not None else _env_float("AI_BRIDGE_LOCAL_LLM_HEALTH_TIMEOUT_SEC", 1.0, 0.2))
        generation_timeout = _env_float("AI_BRIDGE_LOCAL_LLM_GENERATE_TIMEOUT_SEC", max(60.0, health_timeout * 30), 5.0)
        management_timeout = _env_float("AI_BRIDGE_LOCAL_LLM_MANAGEMENT_TIMEOUT_SEC", 600.0, 5.0)
        return cls(
            endpoint=_normalize_endpoint(base_endpoint),
            fallback_endpoints=fallback_endpoints,
            model_name=model_name or os.getenv("AI_BRIDGE_LOCAL_LLM_MODEL") or DEFAULT_LOCAL_MODEL,
            health_timeout_sec=health_timeout,
            generation_timeout_sec=generation_timeout,
            management_timeout_sec=management_timeout,
            default_options=_default_generation_options(),
            retry_policy=LocalModelRetryPolicy(
                max_attempts=_env_int("AI_BRIDGE_LOCAL_LLM_RETRY_ATTEMPTS", 2, 1),
                backoff_base_sec=_env_float("AI_BRIDGE_LOCAL_LLM_RETRY_BACKOFF_SEC", 0.2, 0.0),
            ),
        )

    @property
    def endpoints(self) -> tuple[str, ...]:
        return _candidate_endpoints(self.endpoint, self.fallback_endpoints)

    @property
    def default_model(self) -> str:
        return self.model_name

    @default_model.setter
    def default_model(self, value: str) -> None:
        self.model_name = value.strip() or self.model_name

    @property
    def generate_timeout_sec(self) -> float:
        return self.generation_timeout_sec

    @generate_timeout_sec.setter
    def generate_timeout_sec(self, value: float) -> None:
        self.generation_timeout_sec = max(5.0, value)

    def with_model(self, model_name: str) -> LocalModelRuntimeConfig:
        return replace(self, model_name=model_name.strip() or self.model_name)


@dataclass(slots=True)
class LocalModelInfo:
    name: str
    size: int | None = None
    digest: str | None = None
    modified_at: str | None = None
    details: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class LocalModelResidentInfo:
    name: str
    size: int | None = None
    size_vram: int | None = None
    expires_at: str | None = None
    digest: str | None = None
    details: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class LocalModelMetrics:
    endpoint: str
    attempts: int
    latency_sec: float
    latency_ms: float
    wall_time_sec: float
    prompt_eval_count: int = 0
    eval_count: int = 0
    total_duration_sec: float = 0.0
    load_duration_sec: float = 0.0
    prompt_eval_duration_sec: float = 0.0
    eval_duration_sec: float = 0.0
    input_chars: int = 0
    output_chars: int = 0
    finished: bool = False
    done_reason: str | None = None
    error: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class LocalModelHealth:
    ok: bool
    ready: bool
    status: str
    endpoint: str
    model_name: str
    available_models: list[str]
    model_present: bool
    latency_ms: float
    attempts: int
    status_code: int | None = None
    error: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class LocalModelGenerationRequest:
    prompt: str
    model_name: str | None = None
    system: str | None = None
    options: dict[str, Any] | None = None
    timeout_sec: float | None = None
    keep_alive: int | str | None = None
    format: str | dict[str, Any] | None = None
    raw: bool = False
    stream: bool = False


@dataclass(slots=True)
class LocalModelGenerationResult:
    text: str
    model: str
    endpoint: str
    payload: dict[str, Any]
    metrics: LocalModelMetrics

    def as_dict(self) -> dict[str, Any]:
        return {
            "text": self.text,
            "model": self.model,
            "endpoint": self.endpoint,
            "payload": self.payload,
            "metrics": self.metrics.as_dict(),
        }


@dataclass(slots=True)
class _AttemptState:
    attempts: int = 0
    status_code: int | None = None
    error: str | None = None


class LocalPromptBuilder:
    @staticmethod
    def compose(
        objective: str,
        *,
        system_hint: str | None = None,
        sections: Mapping[str, Any] | None = None,
    ) -> str:
        lines = [objective.strip()]
        if system_hint:
            lines.append(f"SYSTEM HINT: {system_hint.strip()}")
        for key, value in (sections or {}).items():
            if value is None:
                continue
            if isinstance(value, (list, tuple, set)):
                rendered = ", ".join(str(item) for item in value if str(item).strip())
            else:
                rendered = str(value).strip()
            if rendered:
                lines.append(f"{str(key).upper()}: {rendered}")
        return "\n".join(lines)


class _LocalModelRuntimeBase:
    def __init__(self, config: LocalModelRuntimeConfig | None = None) -> None:
        self.config = config or LocalModelRuntimeConfig.from_env()
        self._active_endpoint = self.config.endpoint

    @property
    def endpoint(self) -> str:
        return self._active_endpoint

    def _endpoint_order(self) -> tuple[str, ...]:
        ordered = [self._active_endpoint, *self.config.endpoints]
        unique: list[str] = []
        for endpoint in ordered:
            if endpoint not in unique:
                unique.append(endpoint)
        return tuple(unique)

    def _remember_success(self, endpoint: str) -> None:
        self._active_endpoint = endpoint

    @staticmethod
    def _model_matches(expected: str, candidate: str) -> bool:
        expected_base = expected.split(":", 1)[0]
        candidate_base = candidate.split(":", 1)[0]
        return candidate == expected or candidate_base == expected_base

    @staticmethod
    def _extract_models(payload: Any) -> list[LocalModelInfo]:
        models = payload.get("models", []) if isinstance(payload, dict) else []
        items: list[LocalModelInfo] = []
        for item in models if isinstance(models, list) else []:
            if not isinstance(item, dict):
                continue
            name = str(item.get("name") or "").strip()
            if not name:
                continue
            items.append(
                LocalModelInfo(
                    name=name,
                    size=item.get("size") if isinstance(item.get("size"), int) else None,
                    digest=str(item.get("digest")).strip() if item.get("digest") else None,
                    modified_at=str(item.get("modified_at")).strip() if item.get("modified_at") else None,
                    details=dict(item),
                )
            )
        return items

    @staticmethod
    def _extract_residents(payload: Any) -> list[LocalModelResidentInfo]:
        models = payload.get("models", []) if isinstance(payload, dict) else []
        items: list[LocalModelResidentInfo] = []
        for item in models if isinstance(models, list) else []:
            if not isinstance(item, dict):
                continue
            name = str(item.get("name") or item.get("model") or "").strip()
            if not name:
                continue
            items.append(
                LocalModelResidentInfo(
                    name=name,
                    size=item.get("size") if isinstance(item.get("size"), int) else None,
                    size_vram=item.get("size_vram") if isinstance(item.get("size_vram"), int) else None,
                    expires_at=str(item.get("expires_at")).strip() if item.get("expires_at") else None,
                    digest=str(item.get("digest")).strip() if item.get("digest") else None,
                    details=dict(item),
                )
            )
        return items

    @staticmethod
    def _metrics_from_payload(
        *,
        endpoint: str,
        attempts: int,
        wall_time_sec: float,
        prompt: str,
        payload: dict[str, Any],
        text: str,
        error: str | None = None,
    ) -> LocalModelMetrics:
        total_duration_ns = int(payload.get("total_duration") or 0)
        prompt_eval_duration_ns = int(payload.get("prompt_eval_duration") or 0)
        eval_duration_ns = int(payload.get("eval_duration") or 0)
        load_duration_ns = int(payload.get("load_duration") or 0)
        total_duration_sec = float(total_duration_ns) / 1_000_000_000.0 if total_duration_ns > 0 else wall_time_sec
        return LocalModelMetrics(
            endpoint=endpoint,
            attempts=attempts,
            latency_sec=round(total_duration_sec, 6),
            latency_ms=round(total_duration_sec * 1000.0, 3),
            wall_time_sec=round(wall_time_sec, 6),
            prompt_eval_count=int(payload.get("prompt_eval_count") or 0),
            eval_count=int(payload.get("eval_count") or 0),
            total_duration_sec=round(total_duration_sec, 6),
            load_duration_sec=round(float(load_duration_ns) / 1_000_000_000.0, 6) if load_duration_ns > 0 else 0.0,
            prompt_eval_duration_sec=round(float(prompt_eval_duration_ns) / 1_000_000_000.0, 6) if prompt_eval_duration_ns > 0 else 0.0,
            eval_duration_sec=round(float(eval_duration_ns) / 1_000_000_000.0, 6) if eval_duration_ns > 0 else 0.0,
            input_chars=len(prompt),
            output_chars=len(text),
            finished=bool(payload.get("done", True)),
            done_reason=str(payload.get("done_reason")).strip() if payload.get("done_reason") else None,
            error=error,
        )


class LocalModelClient(_LocalModelRuntimeBase):
    def __init__(
        self,
        config: LocalModelRuntimeConfig | None = None,
        *,
        client: httpx.Client | None = None,
    ) -> None:
        super().__init__(config)
        self._client = client or httpx.Client(headers=self.config.default_headers)
        self._owns_client = client is None

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def __enter__(self) -> LocalModelClient:
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()

    def _request(
        self,
        method: str,
        path: str,
        *,
        json_body: dict[str, Any] | None = None,
        timeout_sec: float,
        retry_policy: LocalModelRetryPolicy | None = None,
    ) -> tuple[httpx.Response, str, int]:
        policy = retry_policy or self.config.retry_policy
        state = _AttemptState()
        for endpoint in self._endpoint_order():
            url = f"{endpoint}{path}"
            for attempt in range(1, policy.max_attempts + 1):
                state.attempts += 1
                try:
                    response = self._client.request(method, url, json=json_body, timeout=timeout_sec)
                    state.status_code = response.status_code
                    if response.status_code >= 400 and policy.should_retry_status(response.status_code) and attempt < policy.max_attempts:
                        if policy.backoff_base_sec > 0:
                            time.sleep(policy.backoff_base_sec * attempt)
                        continue
                    response.raise_for_status()
                    self._remember_success(endpoint)
                    return response, endpoint, state.attempts
                except httpx.HTTPStatusError as exc:
                    state.error = str(exc)
                    if not policy.should_retry_status(exc.response.status_code) or attempt >= policy.max_attempts:
                        break
                    if policy.backoff_base_sec > 0:
                        time.sleep(policy.backoff_base_sec * attempt)
                except httpx.HTTPError as exc:
                    state.error = str(exc)
                    if not policy.retry_transport_errors or attempt >= policy.max_attempts:
                        break
                    if policy.backoff_base_sec > 0:
                        time.sleep(policy.backoff_base_sec * attempt)
            if state.status_code and state.status_code < 500:
                break
        raise RuntimeError(state.error or "local_model_request_failed")

    def list_models(self) -> list[LocalModelInfo]:
        response, _endpoint, _attempts = self._request("GET", "/api/tags", timeout_sec=self.config.health_timeout_sec)
        payload = response.json() if response.content else {}
        return self._extract_models(payload)

    def list_resident_models(self) -> list[LocalModelResidentInfo]:
        response, _endpoint, _attempts = self._request("GET", "/api/ps", timeout_sec=self.config.health_timeout_sec)
        payload = response.json() if response.content else {}
        return self._extract_residents(payload)

    def health(self, model_name: str | None = None) -> LocalModelHealth:
        start = time.perf_counter()
        try:
            response, endpoint, attempts = self._request("GET", "/api/tags", timeout_sec=self.config.health_timeout_sec)
            payload = response.json() if response.content else {}
            models = self._extract_models(payload)
            target_model = (model_name or self.config.model_name).strip()
            available_models = [item.name for item in models]
            model_present = any(self._model_matches(target_model, item.name) for item in models)
            ready = bool(available_models) and model_present
            return LocalModelHealth(
                ok=True,
                ready=ready,
                status="ready" if ready else "degraded",
                endpoint=endpoint,
                model_name=target_model,
                available_models=available_models,
                model_present=model_present,
                latency_ms=round((time.perf_counter() - start) * 1000.0, 3),
                attempts=attempts,
                status_code=response.status_code,
            )
        except Exception as exc:
            target_model = (model_name or self.config.model_name).strip()
            return LocalModelHealth(
                ok=False,
                ready=False,
                status="offline",
                endpoint=self.endpoint,
                model_name=target_model,
                available_models=[],
                model_present=False,
                latency_ms=round((time.perf_counter() - start) * 1000.0, 3),
                attempts=0,
                error=str(exc),
            )

    def readiness(self, model_name: str | None = None) -> dict[str, Any]:
        health = self.health(model_name)
        return {
            "ok": health.ready,
            "service_reachable": health.ok,
            "model_present": health.model_present,
            "model_name": health.model_name,
            "status": health.status,
            "status_code": health.status_code,
            "available_models": health.available_models,
            "error": health.error,
            "endpoint": health.endpoint,
            "attempts": health.attempts,
        }

    def pull_model(self, model_name: str | None = None) -> LocalModelHealth:
        target_model = (model_name or self.config.model_name).strip()
        self._request(
            "POST",
            "/api/pull",
            json_body={"name": target_model, "stream": False},
            timeout_sec=self.config.management_timeout_sec,
            retry_policy=replace(self.config.retry_policy, max_attempts=max(1, self.config.retry_policy.max_attempts)),
        )
        return self.health(target_model)

    def unload_model(self, model_name: str | None = None) -> LocalModelHealth:
        target_model = (model_name or self.config.model_name).strip()
        self._request(
            "POST",
            "/api/generate",
            json_body={"model": target_model, "prompt": "", "template": "", "stream": False, "keep_alive": 0},
            timeout_sec=self.config.health_timeout_sec,
        )
        return self.health(target_model)

    def warm_model(
        self,
        model_name: str | None = None,
        *,
        keep_alive: int | str | None = None,
        timeout_sec: float | None = None,
    ) -> LocalModelGenerationResult:
        target_model = (model_name or self.config.model_name).strip()
        return self.generate(
            LocalModelGenerationRequest(
                prompt="",
                model_name=target_model,
                system="Warm the model and return no content.",
                options={"temperature": 0},
                timeout_sec=timeout_sec or self.config.health_timeout_sec,
                keep_alive=keep_alive if keep_alive is not None else 300,
            )
        )

    def generate(self, request: LocalModelGenerationRequest) -> LocalModelGenerationResult:
        target_model = (request.model_name or self.config.model_name).strip()
        start = time.perf_counter()
        response, endpoint, attempts = self._request(
            "POST",
            "/api/generate",
            json_body={
                "model": target_model,
                "prompt": request.prompt,
                "system": request.system or self.config.default_system,
                "stream": request.stream,
                "options": request.options or dict(self.config.default_options),
                **({"keep_alive": request.keep_alive} if request.keep_alive is not None else {}),
                **({"format": request.format} if request.format is not None else {}),
                **({"raw": request.raw} if request.raw else {}),
            },
            timeout_sec=request.timeout_sec or self.config.generation_timeout_sec,
        )
        payload = response.json() if response.content else {}
        text = str(payload.get("response") or "").strip() if isinstance(payload, dict) else ""
        metrics = self._metrics_from_payload(
            endpoint=endpoint,
            attempts=attempts,
            wall_time_sec=time.perf_counter() - start,
            prompt=request.prompt,
            payload=payload if isinstance(payload, dict) else {},
            text=text,
        )
        return LocalModelGenerationResult(text=text, model=target_model, endpoint=endpoint, payload=payload if isinstance(payload, dict) else {}, metrics=metrics)

    def generate_text(self, prompt: str, **kwargs: Any) -> tuple[str, LocalModelMetrics]:
        result = self.generate(LocalModelGenerationRequest(prompt=prompt, **kwargs))
        return result.text, result.metrics


class AsyncLocalModelClient(_LocalModelRuntimeBase):
    def __init__(
        self,
        config: LocalModelRuntimeConfig | None = None,
        *,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        super().__init__(config)
        self._client = client or httpx.AsyncClient(headers=self.config.default_headers)
        self._owns_client = client is None

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def __aenter__(self) -> AsyncLocalModelClient:
        return self

    async def __aexit__(self, *_args: object) -> None:
        await self.aclose()

    async def _request(
        self,
        method: str,
        path: str,
        *,
        json_body: dict[str, Any] | None = None,
        timeout_sec: float,
        retry_policy: LocalModelRetryPolicy | None = None,
    ) -> tuple[httpx.Response, str, int]:
        policy = retry_policy or self.config.retry_policy
        state = _AttemptState()
        for endpoint in self._endpoint_order():
            url = f"{endpoint}{path}"
            for attempt in range(1, policy.max_attempts + 1):
                state.attempts += 1
                try:
                    response = await self._client.request(method, url, json=json_body, timeout=timeout_sec)
                    state.status_code = response.status_code
                    if response.status_code >= 400 and policy.should_retry_status(response.status_code) and attempt < policy.max_attempts:
                        if policy.backoff_base_sec > 0:
                            await asyncio.sleep(policy.backoff_base_sec * attempt)
                        continue
                    response.raise_for_status()
                    self._remember_success(endpoint)
                    return response, endpoint, state.attempts
                except httpx.HTTPStatusError as exc:
                    state.error = str(exc)
                    if not policy.should_retry_status(exc.response.status_code) or attempt >= policy.max_attempts:
                        break
                    if policy.backoff_base_sec > 0:
                        await asyncio.sleep(policy.backoff_base_sec * attempt)
                except httpx.HTTPError as exc:
                    state.error = str(exc)
                    if not policy.retry_transport_errors or attempt >= policy.max_attempts:
                        break
                    if policy.backoff_base_sec > 0:
                        await asyncio.sleep(policy.backoff_base_sec * attempt)
            if state.status_code and state.status_code < 500:
                break
        raise RuntimeError(state.error or "local_model_request_failed")

    async def list_models(self) -> list[LocalModelInfo]:
        response, _endpoint, _attempts = await self._request("GET", "/api/tags", timeout_sec=self.config.health_timeout_sec)
        payload = response.json() if response.content else {}
        return self._extract_models(payload)

    async def list_resident_models(self) -> list[LocalModelResidentInfo]:
        response, _endpoint, _attempts = await self._request("GET", "/api/ps", timeout_sec=self.config.health_timeout_sec)
        payload = response.json() if response.content else {}
        return self._extract_residents(payload)

    async def health(self, model_name: str | None = None) -> LocalModelHealth:
        start = time.perf_counter()
        try:
            response, endpoint, attempts = await self._request("GET", "/api/tags", timeout_sec=self.config.health_timeout_sec)
            payload = response.json() if response.content else {}
            models = self._extract_models(payload)
            target_model = (model_name or self.config.model_name).strip()
            available_models = [item.name for item in models]
            model_present = any(self._model_matches(target_model, item.name) for item in models)
            ready = bool(available_models) and model_present
            return LocalModelHealth(
                ok=True,
                ready=ready,
                status="ready" if ready else "degraded",
                endpoint=endpoint,
                model_name=target_model,
                available_models=available_models,
                model_present=model_present,
                latency_ms=round((time.perf_counter() - start) * 1000.0, 3),
                attempts=attempts,
                status_code=response.status_code,
            )
        except Exception as exc:
            target_model = (model_name or self.config.model_name).strip()
            return LocalModelHealth(
                ok=False,
                ready=False,
                status="offline",
                endpoint=self.endpoint,
                model_name=target_model,
                available_models=[],
                model_present=False,
                latency_ms=round((time.perf_counter() - start) * 1000.0, 3),
                attempts=0,
                error=str(exc),
            )

    async def readiness(self, model_name: str | None = None) -> dict[str, Any]:
        health = await self.health(model_name)
        return {
            "ok": health.ready,
            "service_reachable": health.ok,
            "model_present": health.model_present,
            "model_name": health.model_name,
            "status": health.status,
            "status_code": health.status_code,
            "available_models": health.available_models,
            "error": health.error,
            "endpoint": health.endpoint,
            "attempts": health.attempts,
        }

    async def pull_model(self, model_name: str | None = None) -> LocalModelHealth:
        target_model = (model_name or self.config.model_name).strip()
        await self._request(
            "POST",
            "/api/pull",
            json_body={"name": target_model, "stream": False},
            timeout_sec=self.config.management_timeout_sec,
            retry_policy=replace(self.config.retry_policy, max_attempts=max(1, self.config.retry_policy.max_attempts)),
        )
        return await self.health(target_model)

    async def unload_model(self, model_name: str | None = None) -> LocalModelHealth:
        target_model = (model_name or self.config.model_name).strip()
        await self._request(
            "POST",
            "/api/generate",
            json_body={"model": target_model, "prompt": "", "template": "", "stream": False, "keep_alive": 0},
            timeout_sec=self.config.health_timeout_sec,
        )
        return await self.health(target_model)

    async def warm_model(
        self,
        model_name: str | None = None,
        *,
        keep_alive: int | str | None = None,
        timeout_sec: float | None = None,
    ) -> LocalModelGenerationResult:
        target_model = (model_name or self.config.model_name).strip()
        return await self.generate(
            LocalModelGenerationRequest(
                prompt="",
                model_name=target_model,
                system="Warm the model and return no content.",
                options={"temperature": 0},
                timeout_sec=timeout_sec or self.config.health_timeout_sec,
                keep_alive=keep_alive if keep_alive is not None else 300,
            )
        )

    async def generate(self, request: LocalModelGenerationRequest) -> LocalModelGenerationResult:
        target_model = (request.model_name or self.config.model_name).strip()
        start = time.perf_counter()
        response, endpoint, attempts = await self._request(
            "POST",
            "/api/generate",
            json_body={
                "model": target_model,
                "prompt": request.prompt,
                "system": request.system or self.config.default_system,
                "stream": request.stream,
                "options": request.options or dict(self.config.default_options),
                **({"keep_alive": request.keep_alive} if request.keep_alive is not None else {}),
                **({"format": request.format} if request.format is not None else {}),
                **({"raw": request.raw} if request.raw else {}),
            },
            timeout_sec=request.timeout_sec or self.config.generation_timeout_sec,
        )
        payload = response.json() if response.content else {}
        text = str(payload.get("response") or "").strip() if isinstance(payload, dict) else ""
        metrics = self._metrics_from_payload(
            endpoint=endpoint,
            attempts=attempts,
            wall_time_sec=time.perf_counter() - start,
            prompt=request.prompt,
            payload=payload if isinstance(payload, dict) else {},
            text=text,
        )
        return LocalModelGenerationResult(text=text, model=target_model, endpoint=endpoint, payload=payload if isinstance(payload, dict) else {}, metrics=metrics)

    async def generate_text(self, prompt: str, **kwargs: Any) -> tuple[str, LocalModelMetrics]:
        result = await self.generate(LocalModelGenerationRequest(prompt=prompt, **kwargs))
        return result.text, result.metrics


class LocalModelRuntime:
    @staticmethod
    def _model_matches(expected: str, candidate: str) -> bool:
        return _LocalModelRuntimeBase._model_matches(expected, candidate)

    def __init__(self, config: LocalModelRuntimeConfig | None = None) -> None:
        self.config = config or LocalModelRuntimeConfig.from_env()
        self._sync = LocalModelClient(self.config)

    @property
    def current_endpoint(self) -> str:
        return self._sync.endpoint

    def close(self) -> None:
        self._sync.close()

    def check_health_sync(self, model_name: str | None = None) -> LocalModelHealth:
        return self._sync.health(model_name)

    async def check_health(self, model_name: str | None = None) -> LocalModelHealth:
        async with AsyncLocalModelClient(self.config) as client:
            return await client.health(model_name)

    def list_models_sync(self) -> list[LocalModelInfo]:
        return self._sync.list_models()

    async def list_models(self) -> list[LocalModelInfo]:
        async with AsyncLocalModelClient(self.config) as client:
            return await client.list_models()

    def list_resident_models_sync(self) -> list[LocalModelResidentInfo]:
        return self._sync.list_resident_models()

    async def list_resident_models(self) -> list[LocalModelResidentInfo]:
        async with AsyncLocalModelClient(self.config) as client:
            return await client.list_resident_models()

    def pull_model_sync(self, model_name: str | None = None, timeout_sec: float | None = None) -> bool:
        if timeout_sec is not None:
            self.config.management_timeout_sec = max(5.0, timeout_sec)
        return self._sync.pull_model(model_name).ok

    async def pull_model(self, model_name: str | None = None) -> LocalModelHealth:
        async with AsyncLocalModelClient(self.config) as client:
            return await client.pull_model(model_name)

    def unload_model_sync(self, model_name: str | None = None) -> bool:
        self._sync.unload_model(model_name)
        return True

    async def unload_model(self, model_name: str | None = None) -> LocalModelHealth:
        async with AsyncLocalModelClient(self.config) as client:
            return await client.unload_model(model_name)

    def warm_model_sync(
        self,
        model_name: str | None = None,
        *,
        keep_alive: int | str | None = None,
        timeout_sec: float | None = None,
    ) -> LocalModelGenerationResult:
        return self._sync.warm_model(model_name, keep_alive=keep_alive, timeout_sec=timeout_sec)

    async def warm_model(
        self,
        model_name: str | None = None,
        *,
        keep_alive: int | str | None = None,
        timeout_sec: float | None = None,
    ) -> LocalModelGenerationResult:
        async with AsyncLocalModelClient(self.config) as client:
            return await client.warm_model(model_name, keep_alive=keep_alive, timeout_sec=timeout_sec)

    def generate_sync(
        self,
        prompt: str,
        model_name: str | None = None,
        *,
        system: str | None = None,
        options: dict[str, Any] | None = None,
        timeout_sec: float | None = None,
        keep_alive: int | str | None = None,
        format: str | dict[str, Any] | None = None,
    ) -> LocalModelGenerationResult:
        return self._sync.generate(
            LocalModelGenerationRequest(
                prompt=prompt,
                model_name=model_name,
                system=system,
                options=options,
                timeout_sec=timeout_sec,
                keep_alive=keep_alive,
                format=format,
            )
        )

    async def generate(
        self,
        prompt: str,
        model_name: str | None = None,
        *,
        system: str | None = None,
        options: dict[str, Any] | None = None,
        timeout_sec: float | None = None,
        keep_alive: int | str | None = None,
        format: str | dict[str, Any] | None = None,
    ) -> LocalModelGenerationResult:
        async with AsyncLocalModelClient(self.config) as client:
            return await client.generate(
                LocalModelGenerationRequest(
                    prompt=prompt,
                    model_name=model_name,
                    system=system,
                    options=options,
                    timeout_sec=timeout_sec,
                    keep_alive=keep_alive,
                    format=format,
                )
            )


__all__ = [
    "AsyncLocalModelClient",
    "DEFAULT_GENERATION_SYSTEM",
    "DEFAULT_LOCAL_ENDPOINT",
    "DEFAULT_LOCAL_MODEL",
    "LocalModelClient",
    "LocalModelGenerationRequest",
    "LocalModelGenerationResult",
    "LocalModelHealth",
    "LocalModelInfo",
    "LocalModelResidentInfo",
    "LocalModelMetrics",
    "LocalModelRetryPolicy",
    "LocalModelRuntime",
    "LocalModelRuntimeConfig",
    "LocalPromptBuilder",
]
