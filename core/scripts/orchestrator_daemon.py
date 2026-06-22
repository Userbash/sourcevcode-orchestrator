import asyncio
import logging
import sys
import os
import threading
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
load_env_file(".env.gemini.local", override=True)
load_env_file("/app/.env.bridge")

from core.core.orchestrator import Orchestrator
from core.agents.planner_agent import PlannerAgent
from core.agents.codex_agent import CodexAgent
from core.agents.antigravity_cli_agent import AntigravityCLIAgent
from core.agents.mistral_agent import MistralAgent
from core.agents.reviewer_agent import ReviewerAgent
from core.agents.tester_agent import TesterAgent
from core.agents.local_llm_agent import LocalLLMAgent
from core.agents.ai_kernel_agent import AIKernelAgent
from core.agents.mimo_agent import MimoAgent
from core.core.orchestration_config import OrchestrationConfig
from core.core.security import SecurityManager, SecurityPolicy
from core.core.provider_credentials import has_usable_credential

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger("orchestrator_daemon")


def _attach_optional_degraded_agents() -> bool:
    return os.getenv("AI_BRIDGE_ATTACH_OPTIONAL_DEGRADED_AGENTS", "false").strip().lower() in {"1", "true", "yes", "on"}


def _attach_optional_local_agent(
    orchestrator: Orchestrator,
    agent_id: str,
    agent,
    *,
    agent_type: str,
    model_name: str,
    provider: str,
    critical: bool = False,
) -> bool:
    try:
        health = agent.health()
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


def _build_http_app(orchestrator: Orchestrator):
    from fastapi import FastAPI, Query, WebSocket, WebSocketDisconnect
    from fastapi.responses import JSONResponse

    app = FastAPI(title="AI Orchestrator Kernel API")
    _orch = orchestrator

    def _negotiate_subprotocol(websocket) -> str | None:
        requested = websocket.headers.get("sec-websocket-protocol", "")
        supported = ("chat.v1", "chat.json")
        for item in requested.split(","):
            candidate = item.strip()
            if candidate in supported:
                return candidate
        return None

    @app.get("/health")
    def health():
        return {"status": "ok", "service": "orchestrator", "ts": datetime.now(UTC).isoformat()}

    @app.get("/health/full")
    def health_full():
        try:
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
            from core.core.antigravity_status_module import shared_antigravity_snapshot
            return shared_antigravity_snapshot(force=False)
        except Exception as exc:
            return {"status": "error", "error": str(exc)}

    @app.get("/stats")
    def stats():
        usage_mod = _orch.module_manager.get_module("model_usage")
        local_model_manager = _orch.module_manager.get_module("local_model_manager")
        module_state = _orch.module_state()
        return {
            "status": "success",
            "data": {
                "model_usage": usage_mod.finalize() if usage_mod and hasattr(usage_mod, "finalize") else {},
                "local_model_manager": local_model_manager.finalize() if local_model_manager and hasattr(local_model_manager, "finalize") else {},
                "provider_inventory": module_state.get("provider_inventory", {}),
            },
        }

    @app.get("/providers/openai/runtime_inventory")
    def openai_runtime_inventory(force_refresh: bool = False, probe_limit: int | None = None):
        try:
            payload = _orch.provider_inventory.refresh_openai_runtime_inventory(force_refresh=force_refresh, probe_limit=probe_limit)
            return {"status": "ok", "data": payload}
        except Exception as exc:
            return JSONResponse({"status": "error", "error": str(exc)}, status_code=500)

    @app.get("/health/local_models")
    def local_model_health():
        local_model_manager = _orch.module_manager.get_module("local_model_manager")
        if local_model_manager and hasattr(local_model_manager, "finalize"):
            state = local_model_manager.finalize()
            return {
                "status": "ok",
                "resident_models": state.get("resident_models", []),
                "blocked_models": state.get("blocked_models", []),
                "memory_pressure": state.get("memory_pressure", {}),
                "evictions": state.get("evictions", 0),
                "warmups": state.get("warmups", 0),
            }
        return JSONResponse({"status": "error", "error": "local_model_manager not loaded"}, status_code=503)

    @app.get("/dump_memory")
    def dump_memory():
        try:
            return {"status": "ok", "modules": _orch.module_manager.loaded_modules()}
        except Exception as exc:
            return {"status": "error", "error": str(exc)}

    @app.get("/sourcecraft")
    def sourcecraft_status():
        try:
            module = _orch.get_module("sourcecraft")
            if not module:
                return JSONResponse({"status": "error", "error": "sourcecraft module not loaded"}, status_code=503)

            runtime_repo_path = module._default_repo_path() if hasattr(module, "_default_repo_path") else "."
            runtime = module.ensure_ready(repo_path=runtime_repo_path) if hasattr(module, "ensure_ready") else {"status": "unknown"}
            profile = module._role_profile().as_dict() if hasattr(module, "_role_profile") else {}
            final = module.finalize() if hasattr(module, "finalize") else {}
            return {
                "status": final.get("status", runtime.get("status", "unknown")),
                "runtime": runtime,
                "role": profile,
                "module": final,
            }
        except Exception as exc:
            return JSONResponse({"status": "error", "error": str(exc)}, status_code=500)

    @app.post("/sourcecraft/delegate")
    def sourcecraft_delegate(payload: dict):
        from core.core.task_submission_api import create_standard_task

        try:
            task = create_standard_task(payload)
            module = _orch.get_module("sourcecraft")
            if not module:
                return JSONResponse({"status": "error", "error": "sourcecraft module not loaded"}, status_code=503)

            delegation = module.build_delegation_profile(task, payload) if hasattr(module, "build_delegation_profile") else {}
            acceptance = _orch.router.route(task)
            route = acceptance.as_dict() if hasattr(acceptance, "as_dict") else acceptance
            assigned_agent = route.get("assigned_agent") if isinstance(route, dict) else None
            route_mode = "orchestrator" if assigned_agent == "orchestrator" else "p2p"
            schedule = {
                "task_id": task.task_id,
                "route_mode": route_mode,
                "assigned_agent": assigned_agent,
                "requires_orchestrator": route_mode == "orchestrator",
                "reason": route.get("message") if isinstance(route, dict) else None,
            }
            return {
                "status": "ok",
                "delegation": delegation,
                "route": route,
                "schedule": schedule,
            }
        except Exception as exc:
            return JSONResponse({"status": "error", "error": str(exc)}, status_code=400)

    @app.get("/diagnostics")
    async def diagnostics(layer: list[str] | None = Query(default=None), matrix_only: bool = False):
        try:
            diag_module = _orch.get_module("self_diagnostic") if hasattr(_orch, "get_module") else None
            if not diag_module:
                return JSONResponse(
                    {
                        "status": "error",
                        "error": "self_diagnostic module not found",
                        "failure_code": "SELF_DIAGNOSTIC_MODULE_UNAVAILABLE",
                    },
                    status_code=503,
                )
            payload = await diag_module.run_diagnostics(layers=layer or None, matrix_only=matrix_only)
            if not isinstance(payload, dict):
                return JSONResponse(
                    {
                        "status": "error",
                        "error": "diagnostics payload is not a JSON object",
                        "failure_code": "HTTP_DIAGNOSTICS_PAYLOAD_INVALID",
                    },
                    status_code=500,
                )
            required = {"schema_version", "layers", "matrix"} if matrix_only else {"schema_version"}
            missing = sorted(field for field in required if field not in payload)
            if missing:
                return JSONResponse(
                    {
                        "status": "error",
                        "error": f"diagnostics payload missing required fields: {', '.join(missing)}",
                        "failure_code": "HTTP_DIAGNOSTICS_PAYLOAD_INVALID",
                    },
                    status_code=500,
                )
            return JSONResponse(payload)
        except Exception as exc:
            return JSONResponse(
                {
                    "status": "error",
                    "error": str(exc),
                    "failure_code": "HTTP_DIAGNOSTICS_PAYLOAD_INVALID",
                },
                status_code=500,
            )

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

                command = frame.get("u") or frame.get("message") or frame.get("text") or ""
                session_id = frame.get("m") or frame.get("session_id") or connection_session_id
                user_id = frame.get("v") or frame.get("user_id") or "ws-user"
                source = frame.get("s") or frame.get("source") or "websocket"
                provider = frame.get("o") or frame.get("provider") or ""
                priority = frame.get("priority")
                cost_tier = frame.get("cost_tier") or frame.get("tier")
                model = frame.get("model") or frame.get("requested_model")
                complexity = frame.get("complexity")

                if not command:
                    await websocket.send_text(json.dumps({"type": "error", "error": "empty_message"}))
                    continue

                task_payload = {
                    "message": command,
                    "description": command,
                    "session_id": session_id,
                    "user_id": user_id,
                    "source": source,
                }
                if provider:
                    task_payload["provider"] = provider
                if priority:
                    task_payload["priority"] = priority
                if cost_tier:
                    task_payload["cost_tier"] = cost_tier
                if model:
                    task_payload["model"] = model
                if complexity:
                    task_payload["complexity"] = complexity

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
    port = int(os.getenv("ORCHESTRATOR_PORT", "8000"))
    config = uvicorn.Config(app, host="0.0.0.0", port=port, log_level="warning")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    logger.info(f"[HTTP] FastAPI server started on port {port}")


async def main():
    logger.info("Initializing Orchestrator daemon and binding agents...")

    orchestrator = Orchestrator()
    orchestrator.orchestration_config = OrchestrationConfig.from_env()

    security_manager = SecurityManager(SecurityPolicy(allow_shell=True, shell_allowlist=["agy -p", "antigravity -p"]))

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

    try:
        orchestrator._refresh_provider_inventory_snapshot(force_refresh=True)
    except Exception as exc:
        logger.warning(f"[INVENTORY] warm refresh before worker attach failed: {exc}")

    orchestrator.attach_local_agent("planner-1", PlannerAgent("planner-1"), agent_type="planner", critical=True, model_name="gpt-planner", provider="openai")
    orchestrator.attach_local_agent("codex-main", CodexAgent("codex-main"), agent_type="codex", critical=True, model_name=codex_model, provider=codex_provider)
    worker_sync = orchestrator.sync_openai_template_workers(enabled=openai_key, primary_model=codex_model)
    if worker_sync.get("attached") or worker_sync.get("removed"):
        logger.info(f"[AGENTS] OpenAI-compatible worker sync: {worker_sync}")
    _attach_optional_local_agent(
        orchestrator,
        "antigravity-cli-1",
        AntigravityCLIAgent("antigravity-cli-1", security_manager),
        agent_type="external_ai",
        critical=False,
        model_name="antigravity-cli",
        provider="google",
    )
    _attach_optional_local_agent(
        orchestrator,
        "mistral-1",
        MistralAgent("mistral-1", security_manager),
        agent_type="external_ai",
        critical=False,
        model_name="mistral-large-latest",
        provider="mistral",
    )
    orchestrator.attach_local_agent("tester-1", TesterAgent("tester-1"), agent_type="tester", model_name="gpt-test-standard", provider="openai")
    orchestrator.attach_local_agent("reviewer-1", ReviewerAgent("reviewer-1"), agent_type="reviewer", model_name="gpt-review-large", provider="openai")
    _attach_optional_local_agent(
        orchestrator,
        "local-llm-1",
        LocalLLMAgent("local-llm-1", os.getenv("AI_BRIDGE_LOCAL_LLM_MODEL", "qwen2.5:32b-instruct-q4_k_m")),
        agent_type="custom",
        critical=False,
        model_name=os.getenv("AI_BRIDGE_LOCAL_LLM_MODEL", "qwen2.5:32b-instruct-q4_k_m"),
        provider="local",
    )
    _attach_optional_local_agent(
        orchestrator,
        "ai-kernel-qwen36-1",
        AIKernelAgent("ai-kernel-qwen36-1"),
        agent_type="custom",
        critical=False,
        model_name=os.getenv("AI_KERNEL_MODEL_ALIAS", "hauhaucs-qwen36-35b-a3b-aggressive:q4_k_m"),
        provider="ai_kernel",
    )
    mimo_agent = MimoAgent("mimo-router-1", default_model=os.getenv("AI_BRIDGE_MIMO_DEFAULT_MODEL", "mimo/mimo-auto"))
    if mimo_agent._cli_path():
        _attach_optional_local_agent(
            orchestrator,
            "mimo-router-1",
            mimo_agent,
            agent_type="external_ai",
            critical=False,
            model_name=os.getenv("AI_BRIDGE_MIMO_DEFAULT_MODEL", "mimo/mimo-auto"),
            provider="mimo",
        )

    _start_http_server(orchestrator)

    logger.info(f"System Ready. Agents bound: {len(orchestrator.registry.list_agents())}")
    await orchestrator.listen_for_tasks()


if __name__ == "__main__":
    asyncio.run(main())
