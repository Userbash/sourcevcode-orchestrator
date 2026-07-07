import asyncio
import json
import logging
import sys
import os
import re
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from datetime import UTC, datetime
from uuid import uuid4

try:
    import core.core.fix_imports
except ImportError:
    pass

sys.path.insert(0, '/app')
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from core.core.env_loader import load_env_file

load_env_file()
load_env_file(".env.bridge", override=True)
load_env_file(".env.local.secrets", override=True)
load_env_file(".env.gemini.local", override=True)
load_env_file("/app/.env.bridge")

from core.core.orchestrator import Orchestrator
from core.agents.planner_agent import PlannerAgent
from core.agents.codex_agent import CodexAgent
from core.agents.distributed_coder_agent import DistributedCoderAgent
from core.agents.result_merger_agent import ResultMergerAgent
from core.agents.antigravity_cli_agent import AntigravityCLIAgent
from core.agents.mistral_agent import MistralAgent
from core.agents.reviewer_agent import ReviewerAgent
from core.agents.tester_agent import TesterAgent
from core.agents.local_llm_agent import LocalLLMAgent
from core.agents.ai_kernel_agent import AIKernelAgent
from core.agents.mimo_agent import MimoAgent
from core.core.orchestration_config import OrchestrationConfig
from core.core.orchestrator_transport import (
    ai_kernel_ensure_payload,
    ai_kernel_gate_payload,
    antigravity_status_payload,
    diagnostics_payload,
    dump_memory_payload,
    health_payload,
    local_llm_connect_payload,
    local_llm_disconnect_payload,
    local_llm_residents_payload,
    local_llm_warm_payload,
    local_model_health_payload,
    openai_discovery_payload,
    openai_model_templates_payload,
    openai_runtime_inventory_payload,
    provider_inventory_payload,
    provider_inventory_single_payload,
    provider_inventory_stream,
    provider_model_lookup_payload,
    provider_models_index_payload,
    provider_models_index_stream,
    provider_runtime_inventory_all_payload,
    provider_runtime_inventory_single_payload,
    provider_runtime_inventory_stream,
    runtime_events_stream,
    socraticode_context_compaction_status_payload,
    socraticode_context_compaction_status_stream,
    sourcecraft_delegate_payload,
    sourcecraft_parallel_delegate_payload,
    sourcecraft_status_payload,
    stats_payload,
    transport_audit_payload,
)
from core.core.orchestrator_ws_dispatcher import build_orchestrator_ws_dispatcher
from core.core.orchestrator_ws_session import OrchestratorWebSocketSession, negotiate_subprotocol
from core.core.ws_protocol import WsEnvelope, WsProtocolValidationError, build_error, parse_envelope
from core.core.security import SecurityManager, SecurityPolicy
from core.core.provider_credentials import has_usable_credential

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger("orchestrator_daemon")

REQUIRED_HTTP_ENDPOINTS = (
    "/health",
    "/health/full",
    "/providers/inventory",
    "/providers/runtime_inventory",
    "/providers/models/index",
    "/providers/ai_kernel/gate",
    "/health/local_models",
    "/sourcecraft",
    "/diagnostics",
)


def _attach_optional_degraded_agents() -> bool:
    return os.getenv("AI_BRIDGE_ATTACH_OPTIONAL_DEGRADED_AGENTS", "false").strip().lower() in {"1", "true", "yes", "on"}


def _attach_placeholder_agents() -> bool:
    return os.getenv("AI_BRIDGE_ATTACH_PLACEHOLDER_AGENTS", "false").strip().lower() in {"1", "true", "yes", "on"}


def _attach_optional_local_agent(
    orchestrator: Orchestrator,
    agent_id: str,
    agent,
    *,
    agent_type: str,
    model_name: str,
    provider: str,
    critical: bool = False,
    health_override=None,
) -> bool:
    try:
        health = health_override if health_override is not None else agent.health()
        status_value = str(getattr(getattr(health, "status", None), "value", getattr(health, "status", "unknown"))).strip().lower()
        if status_value == "ready" or (_attach_optional_degraded_agents() and status_value == "degraded"):
            orchestrator.attach_local_agent(
                agent_id,
                agent,
                agent_type=agent_type,
                critical=critical,
                model_name=model_name,
                provider=provider,
            )
            return True
        logger.warning(
            "[AGENTS] Skipping optional agent %s provider=%s status=%s error=%s",
            agent_id,
            provider,
            status_value,
            getattr(health, "last_error", None),
        )
        return False
    except Exception as exc:
        logger.warning("[AGENTS] Optional agent %s provider=%s startup healthcheck failed: %s", agent_id, provider, exc)
        return False


def _optional_agent_specs(security_manager: SecurityManager) -> list[dict[str, object]]:
    return [
        {
            "agent_id": "antigravity-1",
            "factory": lambda: AntigravityCLIAgent("antigravity-1", security_manager),
            "agent_type": "external_ai",
            "critical": False,
            "model_name": os.getenv("ANTIGRAVITY_DEFAULT_MODEL", "antigravity-pro"),
            "provider": "antigravity",
        },
        {
            "agent_id": "mistral-1",
            "factory": lambda: MistralAgent("mistral-1", security_manager),
            "agent_type": "external_ai",
            "critical": False,
            "model_name": "mistral-large-latest",
            "provider": "mistral",
        },
        {
            "agent_id": "local-llm-1",
            "factory": lambda: LocalLLMAgent("local-llm-1", os.getenv("AI_BRIDGE_LOCAL_LLM_MODEL", "qwen2.5:32b-instruct-q4_k_m")),
            "agent_type": "custom",
            "critical": False,
            "model_name": os.getenv("AI_BRIDGE_LOCAL_LLM_MODEL", "qwen2.5:32b-instruct-q4_k_m"),
            "provider": "local",
        },
        {
            "agent_id": "ai-kernel-qwen36-1",
            "factory": lambda: AIKernelAgent("ai-kernel-qwen36-1"),
            "agent_type": "custom",
            "critical": False,
            "model_name": os.getenv("AI_KERNEL_MODEL_ALIAS", "hauhaucs-qwen36-35b-a3b-aggressive:q4_k_m"),
            "provider": "ai_kernel",
        },
        {
            "agent_id": "mimo-router-1",
            "factory": lambda: MimoAgent("mimo-router-1", default_model=os.getenv("AI_BRIDGE_MIMO_DEFAULT_MODEL", "xiaomi/mimo-v2.5-pro")),
            "agent_type": "external_ai",
            "critical": False,
            "model_name": os.getenv("AI_BRIDGE_MIMO_DEFAULT_MODEL", "xiaomi/mimo-v2.5-pro"),
            "provider": "mimo",
        },
    ]


def _probe_optional_agent(spec: dict[str, object]) -> tuple[dict[str, object], object, object | None, Exception | None]:
    agent = None
    try:
        factory = spec["factory"]
        agent = factory()
        health = agent.health()
        return spec, agent, health, None
    except Exception as exc:
        return spec, agent, None, exc


def _background_startup(orchestrator: Orchestrator, security_manager: SecurityManager, *, openai_key: bool, codex_model: str) -> None:
    try:
        orchestrator._refresh_provider_inventory_snapshot(force_refresh=True)
    except Exception as exc:
        logger.warning(f"[INVENTORY] background warm refresh failed: {exc}")

    try:
        worker_sync = orchestrator.sync_openai_template_workers(enabled=openai_key, primary_model=codex_model)
        if worker_sync.get("attached") or worker_sync.get("removed"):
            logger.info(f"[AGENTS] OpenAI-compatible worker sync: {worker_sync}")
    except Exception as exc:
        logger.warning(f"[AGENTS] background worker sync failed: {exc}")

    specs = _optional_agent_specs(security_manager)
    max_workers = max(1, min(len(specs), int(os.getenv("AI_BRIDGE_OPTIONAL_AGENT_STARTUP_WORKERS", "5") or "5")))
    results: dict[str, tuple[dict[str, object], object, object | None, Exception | None]] = {}

    with ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="optional-agent") as executor:
        future_map = {executor.submit(_probe_optional_agent, spec): str(spec["agent_id"]) for spec in specs}
        for future in as_completed(future_map):
            agent_id = future_map[future]
            try:
                results[agent_id] = future.result()
            except Exception as exc:
                results[agent_id] = ({"agent_id": agent_id}, None, None, exc)

    for spec in specs:
        agent_id = str(spec["agent_id"])
        result_spec, agent, health, exc = results.get(agent_id, (spec, None, None, RuntimeError("missing_startup_probe_result")))
        if exc is not None or agent is None:
            logger.warning("[AGENTS] Optional agent %s provider=%s startup healthcheck failed: %s", agent_id, spec.get("provider"), exc)
            continue
        _attach_optional_local_agent(
            orchestrator,
            agent_id,
            agent,
            agent_type=str(result_spec["agent_type"]),
            critical=bool(result_spec["critical"]),
            model_name=str(result_spec["model_name"]),
            provider=str(result_spec["provider"]),
            health_override=health,
        )

    logger.info(f"[STARTUP] Background startup complete. Agents bound: {len(orchestrator.registry.list_agents())}")


def _launch_background_startup(orchestrator: Orchestrator, security_manager: SecurityManager, *, openai_key: bool, codex_model: str) -> None:
    thread = threading.Thread(
        target=_background_startup,
        kwargs={
            "orchestrator": orchestrator,
            "security_manager": security_manager,
            "openai_key": openai_key,
            "codex_model": codex_model,
        },
        name="orchestrator-background-startup",
        daemon=True,
    )
    thread.start()


def _resolve_http_port() -> int:
    raw = (
        os.getenv("AI_BRIDGE_API_PORT")
        or os.getenv("ORCHESTRATOR_PORT")
        or "8000"
    ).strip()
    try:
        return int(raw)
    except ValueError:
        return 8000


def _assert_required_http_routes(app) -> None:
    routes = {getattr(route, "path", "") for route in getattr(app, "routes", [])}
    missing = [path for path in REQUIRED_HTTP_ENDPOINTS if path not in routes]
    if missing:
        raise RuntimeError(
            "orchestrator http app missing required routes: " + ", ".join(missing)
        )


def _build_http_app(orchestrator: Orchestrator):
    from fastapi import FastAPI, Query, WebSocket, WebSocketDisconnect
    from fastapi.responses import JSONResponse

    app = FastAPI(title="AI Orchestrator Kernel API")
    _orch = orchestrator

    def _response(payload: dict, status_code: int = 200):
        if status_code == 200:
            return payload
        return JSONResponse(payload, status_code=status_code)

    def _legacy_control_ws_metadata(path: str) -> dict[str, str] | None:
        if path == "/stats":
            return {"action": "stats.get"}
        if path == "/antigravity/status":
            return {"action": "antigravity.status.get"}
        if path == "/providers/openai/runtime_inventory":
            return {"action": "providers.openai.runtime_inventory.get", "subscribe": "providers.openai.runtime_inventory.subscribe"}
        if path == "/providers/openai/discovery":
            return {"action": "providers.openai.discovery.get"}
        if path == "/providers/openai/model_templates":
            return {"action": "providers.openai.model_templates.get"}
        if path == "/providers/inventory":
            return {"action": "providers.inventory.get", "subscribe": "providers.inventory.subscribe"}
        if re.fullmatch(r"/providers/[^/]+/inventory", path):
            return {"action": "providers.inventory.provider.get"}
        if path == "/providers/runtime_inventory":
            return {"action": "providers.runtime_inventory.get", "subscribe": "providers.runtime_inventory.subscribe"}
        if re.fullmatch(r"/providers/[^/]+/runtime_inventory", path):
            return {"action": "providers.runtime_inventory.provider.get", "subscribe": "providers.runtime_inventory.provider.subscribe"}
        if path == "/providers/models/index":
            return {"action": "providers.models.index.get", "subscribe": "providers.models.index.subscribe"}
        if path.startswith("/providers/models/index/"):
            return {"action": "providers.models.lookup.get"}
        if path == "/socraticode/context_compaction/status":
            return {"action": "socraticode.context_compaction.status.get", "subscribe": "socraticode.context_compaction.status.subscribe"}
        if path == "/providers/local_llm/residents":
            return {"action": "providers.local_llm.residents.get"}
        if path == "/providers/local_llm/connect":
            return {"action": "providers.local_llm.connect"}
        if path == "/providers/local_llm/disconnect":
            return {"action": "providers.local_llm.disconnect"}
        if path == "/providers/local_llm/warm":
            return {"action": "providers.local_llm.warm"}
        if path == "/providers/ai_kernel/gate":
            return {"action": "providers.ai_kernel.gate.get"}
        if path == "/providers/ai_kernel/ensure":
            return {"action": "providers.ai_kernel.ensure"}
        if path == "/health/local_models":
            return {"action": "health.local_models.get"}
        if path == "/dump_memory":
            return {"action": "memory.dump.get"}
        if path == "/sourcecraft":
            return {"action": "sourcecraft.status.get"}
        if path == "/sourcecraft/delegate":
            return {"action": "sourcecraft.delegate.get", "subscribe": "sourcecraft.delegate"}
        if path == "/sourcecraft/parallel_delegate":
            return {"action": "sourcecraft.parallel_delegate.get", "subscribe": "sourcecraft.parallel_delegate"}
        if path == "/transport/audit":
            return {"action": "transport.audit.get"}
        if path == "/diagnostics":
            return {"action": "diagnostics.get", "subscribe": "diagnostics.subscribe"}
        return None

    @app.middleware("http")
    async def _legacy_control_plane_http_middleware(request, call_next):
        response = await call_next(request)
        metadata = _legacy_control_ws_metadata(request.url.path)
        if metadata is None:
            return response
        response.headers["Deprecation"] = "true"
        response.headers["Link"] = "</control/ws>; rel=alternate"
        response.headers["X-Control-Transport"] = "websocket-primary; compatibility=http"
        response.headers["X-Control-WS-Endpoint"] = "/control/ws"
        response.headers["X-Control-WS-Action"] = metadata["action"]
        subscribe_action = metadata.get("subscribe")
        if subscribe_action:
            response.headers["X-Control-WS-Subscribe"] = subscribe_action
        return response

    def _usage_snapshot() -> dict:
        usage_mod = _orch.module_manager.get_module("model_usage") if hasattr(_orch, "module_manager") else None
        return usage_mod.finalize() if usage_mod and hasattr(usage_mod, "finalize") else {}

    def _suppression_snapshot() -> dict:
        return _orch.provider_budget_router.suppression_snapshot() if hasattr(_orch, "provider_budget_router") else {}

    def _local_llm_module():
        if hasattr(_orch, 'module_manager') and hasattr(_orch.module_manager, 'get_module'):
            return _orch.module_manager.get_module('local_llm')
        if hasattr(_orch, 'get_module'):
            return _orch.get_module('local_llm')
        return None

    def _resident_rows(module) -> list[dict]:
        runtime = getattr(module, 'runtime', None)
        if runtime is None or not hasattr(runtime, 'list_resident_models_sync'):
            return []
        rows = []
        for item in runtime.list_resident_models_sync() or []:
            rows.append({
                'name': str(getattr(item, 'name', '') or ''),
                'size': getattr(item, 'size', None),
                'size_vram': getattr(item, 'size_vram', None),
                'expires_at': getattr(item, 'expires_at', None),
                'digest': getattr(item, 'digest', None),
                'details': getattr(item, 'details', {}) or {},
            })
        return rows

    def _negotiate_subprotocol(websocket) -> str | None:
        return negotiate_subprotocol(websocket, supported_subprotocols=("chat.v1", "chat.json"))

    @app.get("/health")
    def health():
        payload, status_code = health_payload()
        return _response(payload, status_code)

    @app.get("/health/full")
    def health_full():
        try:
            if os.getenv("AI_BRIDGE_AI_KERNEL_HEALTH_AUTOSTART", "true").strip().lower() in {"1", "true", "yes", "on"}:
                try:
                    _orch.ai_kernel_bridge.ensure_ready(os.getenv("AI_KERNEL_MODEL_ALIAS", "hauhaucs-qwen36-35b-a3b-aggressive:q4_k_m"))
                except Exception as exc:
                    logger.warning(f"[HEALTH] AI Kernel ensure_ready failed during /health/full: {exc}")
            full = _orch.healthcheck.check_all()
            module_state = _orch.module_state()
            agent_healths = [h.as_dict() for h in full]
            providers = _orch.healthcheck.check_providers()
            provider_dicts = [v.as_dict() if hasattr(v, 'as_dict') else v for v in providers.values()]
            local_models = module_state.get("local_model_manager", {}) if isinstance(module_state, dict) else {}
            memory_pressure = local_models.get("memory_pressure", {}) if isinstance(local_models, dict) else {}
            blocked_models = local_models.get("blocked_models", []) if isinstance(local_models, dict) else []
            local_ok = not blocked_models and str(memory_pressure.get("pressure_state") or "normal") != "high"
            return {
                "status": "ok",
                "overall_ok": all(p.get("status") in ("healthy", "degraded") for p in provider_dicts) and local_ok,
                "summary": {
                    "provider_count": len(provider_dicts),
                    "agent_count": len(_orch.registry.list_agents()),
                    "ready_agents": sum(1 for a in agent_healths if a.get("status") == "ready"),
                    "problem_agents": sum(1 for a in agent_healths if a.get("status") not in ("ready",)),
                    "problem_providers": sum(1 for p in provider_dicts if p.get("status") not in ("healthy", "degraded")),
                    "blocked_local_models": len(blocked_models),
                    "local_memory_pressure": memory_pressure.get("pressure_state", "unknown"),
                },
                "providers": provider_dicts,
                "agents": agent_healths,
                "modules": module_state,
                "local_models": local_models,
                "sourcecraft": module_state.get("sourcecraft", {}),
                "postgres_state": module_state.get("postgres_state", {}),
                "registry_size": len(_orch.registry.list_agents()),
            }
        except Exception as exc:
                return JSONResponse({"status": "error", "error": str(exc)}, status_code=500)

    @app.get("/api/health")
    def api_health():
        return health()

    @app.get("/antigravity/status")
    def antigravity_status():
        try:
            payload, status_code = antigravity_status_payload(_orch)
            return _response(payload, status_code)
        except Exception as exc:
            return {"status": "error", "error": str(exc)}

    @app.get("/stats")
    def stats():
        payload, status_code = stats_payload(_orch)
        return _response(payload, status_code)

    @app.get("/providers/openai/runtime_inventory")
    def openai_runtime_inventory(force_refresh: bool = False, probe_limit: int | None = None):
        try:
            payload, status_code = openai_runtime_inventory_payload(_orch, force_refresh=force_refresh, probe_limit=probe_limit)
            return _response(payload, status_code)
        except Exception as exc:
                return JSONResponse({"status": "error", "error": str(exc)}, status_code=500)

    @app.get("/providers/openai/discovery")
    def openai_discovery():
        try:
            payload, status_code = openai_discovery_payload(_orch)
            return _response(payload, status_code)
        except Exception as exc:
                return JSONResponse({"status": "error", "error": str(exc)}, status_code=500)

    @app.get("/providers/openai/model_templates")
    def openai_model_templates(force_refresh: bool = False, probe_limit: int | None = None):
        try:
            payload, status_code = openai_model_templates_payload(_orch, force_refresh=force_refresh, probe_limit=probe_limit)
            return _response(payload, status_code)
        except Exception as exc:
                return JSONResponse({"status": "error", "error": str(exc)}, status_code=500)

    @app.get("/providers/inventory")
    def provider_inventory(force_refresh: bool = False):
        try:
            payload, status_code = provider_inventory_payload(_orch, force_refresh=force_refresh)
            return _response(payload, status_code)
        except Exception as exc:
                return JSONResponse({"status": "error", "error": str(exc)}, status_code=500)

    @app.get("/providers/{provider}/inventory")
    def provider_inventory_single(provider: str, force_refresh: bool = False):
        try:
            payload, status_code = provider_inventory_single_payload(_orch, provider, force_refresh=force_refresh)
            return _response(payload, status_code)
        except Exception as exc:
                return JSONResponse({"status": "error", "error": str(exc)}, status_code=500)

    @app.get("/providers/runtime_inventory")
    def provider_runtime_inventory_all(force_refresh: bool = False, probe_limit: int | None = None):
        try:
            payload, status_code = provider_runtime_inventory_all_payload(
                _orch,
                force_refresh=force_refresh,
                probe_limit=probe_limit,
            )
            return _response(payload, status_code)
        except Exception as exc:
                return JSONResponse({"status": "error", "error": str(exc)}, status_code=500)

    @app.get("/providers/{provider}/runtime_inventory")
    def provider_runtime_inventory_single(provider: str, force_refresh: bool = False, probe_limit: int | None = None):
        try:
            payload, status_code = provider_runtime_inventory_single_payload(
                _orch,
                provider,
                force_refresh=force_refresh,
                probe_limit=probe_limit,
            )
            return _response(payload, status_code)
        except Exception as exc:
                return JSONResponse({"status": "error", "error": str(exc)}, status_code=500)

    @app.get("/providers/models/index")
    def provider_models_index(force_refresh: bool = False):
        try:
            payload, status_code = provider_models_index_payload(_orch, force_refresh=force_refresh)
            return _response(payload, status_code)
        except Exception as exc:
                return JSONResponse({"status": "error", "error": str(exc)}, status_code=500)

    @app.get("/providers/models/index/{model_name:path}")
    def provider_model_lookup(model_name: str, force_refresh: bool = False):
        try:
            payload, status_code = provider_model_lookup_payload(_orch, model_name, force_refresh=force_refresh)
            return _response(payload, status_code)
        except Exception as exc:
                return JSONResponse({"status": "error", "error": str(exc)}, status_code=500)

    @app.get("/socraticode/context_compaction/status")
    def socraticode_context_compaction_status():
        try:
            payload, status_code = socraticode_context_compaction_status_payload(_orch)
            return _response(payload, status_code)
        except Exception as exc:
                return JSONResponse({"status": "error", "error": str(exc)}, status_code=500)

    @app.websocket("/ws/providers/inventory")
    async def provider_inventory_ws(websocket: WebSocket):
        protocol = _negotiate_subprotocol(websocket)
        await websocket.accept(subprotocol=protocol)
        try:
            async for event in provider_inventory_stream(_orch):
                await websocket.send_json(event)
        except WebSocketDisconnect:
            return

    @app.websocket("/ws/providers/runtime_inventory")
    async def provider_runtime_inventory_ws(websocket: WebSocket):
        protocol = _negotiate_subprotocol(websocket)
        await websocket.accept(subprotocol=protocol)
        try:
            async for event in provider_runtime_inventory_stream(_orch):
                await websocket.send_json(event)
        except WebSocketDisconnect:
            return

    @app.websocket("/ws/providers/models/index")
    async def provider_models_index_ws(websocket: WebSocket):
        protocol = _negotiate_subprotocol(websocket)
        await websocket.accept(subprotocol=protocol)
        try:
            async for event in provider_models_index_stream(_orch):
                await websocket.send_json(event)
        except WebSocketDisconnect:
            return

    @app.get("/providers/local_llm/residents")
    def local_llm_residents():
        payload, status_code = local_llm_residents_payload(_orch)
        return _response(payload, status_code)

    @app.post("/providers/local_llm/connect")
    def local_llm_connect(payload: dict):
        payload, status_code = local_llm_connect_payload(_orch, payload)
        return _response(payload, status_code)

    @app.post("/providers/local_llm/disconnect")
    def local_llm_disconnect(payload: dict):
        payload, status_code = local_llm_disconnect_payload(_orch, payload)
        return _response(payload, status_code)

    @app.post("/providers/local_llm/warm")
    def local_llm_warm(payload: dict):
        payload, status_code = local_llm_warm_payload(_orch, payload)
        return _response(payload, status_code)

    @app.get("/providers/ai_kernel/gate")
    def ai_kernel_gate(ensure_ready: bool = False, model_name: str | None = None):
        payload, status_code = ai_kernel_gate_payload(_orch, ensure_ready=ensure_ready, model_name=model_name)
        return _response(payload, status_code)

    @app.post("/providers/ai_kernel/ensure")
    def ai_kernel_ensure(payload: dict | None = None):
        payload, status_code = ai_kernel_ensure_payload(_orch, payload or {})
        return _response(payload, status_code)

    @app.get("/health/local_models")
    def local_model_health():
        payload, status_code = local_model_health_payload(_orch)
        return _response(payload, status_code)

    @app.get("/dump_memory")
    def dump_memory():
        try:
            payload, status_code = dump_memory_payload(_orch)
            return _response(payload, status_code)
        except Exception as exc:
            return {"status": "error", "error": str(exc)}

    @app.get("/sourcecraft")
    def sourcecraft_status():
        try:
            payload, status_code = sourcecraft_status_payload(_orch)
            return _response(payload, status_code)
        except Exception as exc:
                return JSONResponse({"status": "error", "error": str(exc)}, status_code=500)

    @app.post("/sourcecraft/delegate")
    def sourcecraft_delegate(payload: dict):
        try:
            body, status_code = sourcecraft_delegate_payload(_orch, payload)
            return _response(body, status_code)
        except Exception as exc:
                return JSONResponse({"status": "error", "error": str(exc)}, status_code=400)

    @app.post("/sourcecraft/parallel_delegate")
    def sourcecraft_parallel_delegate(payload: dict):
        try:
            body, status_code = sourcecraft_parallel_delegate_payload(_orch, payload)
            return _response(body, status_code)
        except Exception as exc:
            return JSONResponse({"status": "error", "error": str(exc)}, status_code=400)


    @app.get("/transport/audit")
    def transport_audit():
        try:
            payload, status_code = transport_audit_payload(_orch)
            return _response(payload, status_code)
        except Exception as exc:
            return JSONResponse({"status": "error", "error": str(exc)}, status_code=500)

    @app.get("/diagnostics")
    async def diagnostics(layer: list[str] | None = Query(default=None), matrix_only: bool = False):
        try:
            payload, status_code = await diagnostics_payload(_orch, layers=layer or None, matrix_only=matrix_only)
            return _response(payload, status_code)
        except Exception as exc:
            return JSONResponse(
                {
                    "status": "error",
                    "error": str(exc),
                    "failure_code": "HTTP_DIAGNOSTICS_PAYLOAD_INVALID",
                },
                status_code=500,
            )

    @app.websocket("/control/ws")
    async def control_ws(websocket: WebSocket):
        dispatcher = build_orchestrator_ws_dispatcher(_orch)
        session = OrchestratorWebSocketSession(websocket, supported_subprotocols=("chat.v1", "chat.json"))
        await session.accept()
        session.start_heartbeat()

        async def _run_request(envelope: WsEnvelope) -> None:
            async for frame in dispatcher.dispatch(envelope):
                await session.send_frame(frame)

        try:
            while True:
                raw = await websocket.receive_text()
                try:
                    frame = json.loads(raw)
                except json.JSONDecodeError:
                    await session.send_frame(build_error(uuid4().hex, "INVALID_JSON", message="frame payload is not valid json"))
                    continue
                if not isinstance(frame, dict):
                    await session.send_frame(build_error(uuid4().hex, "INVALID_FRAME", message="frame payload must be a JSON object"))
                    continue
                frame.setdefault("ack", True)
                if await session.handle_control_frame(frame):
                    continue
                try:
                    envelope = WsEnvelope.from_dict(frame)
                except WsProtocolValidationError as exc:
                    request_id = str(frame.get("request_id") or uuid4().hex)
                    await session.send_frame(
                        build_error(
                            request_id,
                            exc.code,
                            action=str(frame.get("action") or "").strip() or None,
                            correlation_id=str(frame.get("correlation_id") or "").strip() or None,
                            message=str(exc),
                            details=exc.details,
                        )
                    )
                    continue
                task = asyncio.create_task(_run_request(envelope))
                session.track_request(envelope.request_id, task)
                if envelope.type == "subscribe":
                    await session.register_subscription(
                        envelope.request_id,
                        topic=envelope.action or "subscription",
                        metadata={"action": envelope.action},
                        unsubscribe=lambda request_id=envelope.request_id: session.cancel_request(request_id),
                    )
        except WebSocketDisconnect:
            return
        finally:
            await session.close()

    @app.websocket("/ws/runtime/events")
    async def runtime_events_ws(websocket: WebSocket):
        protocol = _negotiate_subprotocol(websocket)
        await websocket.accept(subprotocol=protocol)
        try:
            async for event in runtime_events_stream(_orch):
                await websocket.send_json(event)
        except WebSocketDisconnect:
            return

    @app.websocket("/ws/socraticode/context_compaction/status")
    async def socraticode_context_compaction_status_ws(websocket: WebSocket):
        protocol = _negotiate_subprotocol(websocket)
        await websocket.accept(subprotocol=protocol)
        try:
            async for event in socraticode_context_compaction_status_stream(_orch):
                await websocket.send_json(event)
        except WebSocketDisconnect:
            return

    @app.websocket("/chat/ws")
    async def chat_ws(websocket: WebSocket):
        connection_session_id = f"ws-{uuid4().hex}"
        await websocket.accept(subprotocol=_negotiate_subprotocol(websocket))
        logger.info("[WS] Client connected")
        try:
            while True:
                raw = await websocket.receive_text()
                try:
                    frame = json.loads(raw)
                except json.JSONDecodeError:
                    await websocket.send_text(json.dumps({"type": "error", "error": "invalid_json"}))
                    continue

                try:
                    envelope = parse_envelope(frame, normalize_chat=True)
                except WsProtocolValidationError as exc:
                    await websocket.send_text(json.dumps({"type": "error", "error": exc.code.lower()}))
                    continue

                normalized_data = dict(envelope.data or {})
                command = (
                    normalized_data.get("message")
                    or normalized_data.get("description")
                    or normalized_data.get("text")
                    or normalized_data.get("prompt")
                    or normalized_data.get("objective")
                    or ""
                )
                session_id = normalized_data.get("session_id") or connection_session_id
                user_id = normalized_data.get("user_id") or "ws-user"
                source = normalized_data.get("source") or "websocket"

                if not command:
                    await websocket.send_text(json.dumps({"type": "error", "error": "empty_message"}))
                    continue

                task_payload = dict(normalized_data)
                task_payload["message"] = str(command)
                task_payload.setdefault("description", str(command))
                task_payload["session_id"] = str(session_id)
                task_payload["user_id"] = str(user_id)
                task_payload["source"] = str(source)
                if task_payload.get("tier") and not task_payload.get("cost_tier"):
                    task_payload["cost_tier"] = task_payload.get("tier")
                if task_payload.get("requested_model") and not task_payload.get("model"):
                    task_payload["model"] = task_payload.get("requested_model")

                try:
                    async for event in _orch.stream_user_task(task_payload, source=source):
                        await websocket.send_text(json.dumps(event, default=str))
                except Exception as exc:
                    logger.error(f"[WS] Task failed: {exc}")
                    await websocket.send_text(json.dumps({"type": "final_result", "result": {"status": "failed", "summary": str(exc)}}))
        except WebSocketDisconnect:
            logger.info("[WS] Client disconnected")
        except Exception as exc:
            logger.warning(f"[WS] Client disconnected: {exc}")
        finally:
            logger.info("[WS] Client disconnected")

    return app


def _start_http_server(orchestrator: Orchestrator) -> None:
    import uvicorn

    app = _build_http_app(orchestrator)
    _assert_required_http_routes(app)
    port = _resolve_http_port()
    config = uvicorn.Config(app, host="0.0.0.0", port=port, log_level="warning")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    logger.info(f"[HTTP] FastAPI server started on port {port}")


async def main():
    logger.info("Initializing Orchestrator daemon and binding agents...")

    orchestrator = Orchestrator()
    orchestrator.orchestration_config = OrchestrationConfig.from_env()

    security_manager = SecurityManager(SecurityPolicy())

    mistral_key = has_usable_credential("MISTRAL_API_KEY")
    openai_key = has_usable_credential("OPENAI_API_KEY")
    codex_preference = (os.getenv("AI_BRIDGE_CODEX_PROVIDER") or os.getenv("CODEX_PROVIDER") or "auto").strip().lower()
    if codex_preference == "mistral" and mistral_key:
        codex_provider = "mistral"
        codex_model = os.getenv("CODEX_MISTRAL_MODEL", os.getenv("MISTRAL_MODEL", "codestral-latest"))
    elif codex_preference == "openai" and openai_key:
        codex_provider = "openai"
        codex_model = os.getenv("CODEX_OPENAI_MODEL", "gpt-5.5")
    elif mistral_key:
        codex_provider = "mistral"
        codex_model = os.getenv("CODEX_MISTRAL_MODEL", os.getenv("MISTRAL_MODEL", "codestral-latest"))
    elif openai_key:
        codex_provider = "openai"
        codex_model = os.getenv("CODEX_OPENAI_MODEL", "gpt-5.5")
    else:
        codex_provider = "local"
        codex_model = "local-small"

    if _attach_placeholder_agents():
        orchestrator.attach_local_agent("planner-1", PlannerAgent("planner-1"), agent_type="planner", critical=True, model_name="gpt-planner", provider="openai")
    orchestrator.attach_local_agent("codex-main", CodexAgent("codex-main"), agent_type="codex", critical=True, model_name=codex_model, provider=codex_provider)
    if _attach_placeholder_agents():
        orchestrator.attach_local_agent("tester-1", TesterAgent("tester-1"), agent_type="tester", model_name="gpt-test-standard", provider="openai")
        orchestrator.attach_local_agent("reviewer-1", ReviewerAgent("reviewer-1"), agent_type="reviewer", model_name="gpt-review-large", provider="openai")
    orchestrator.attach_local_agent("distributed-coder-1", DistributedCoderAgent(), agent_type="custom", critical=False, model_name="distributed-coder-core", provider="local")
    orchestrator.attach_local_agent("result-merger", ResultMergerAgent(), agent_type="custom", critical=False, model_name="result-merger-core", provider="local")

    _start_http_server(orchestrator)
    logger.info(f"System Ready. Agents bound: {len(orchestrator.registry.list_agents())}")
    _launch_background_startup(orchestrator, security_manager, openai_key=openai_key, codex_model=codex_model)
    await orchestrator.listen_for_tasks()


if __name__ == "__main__":
    asyncio.run(main())
