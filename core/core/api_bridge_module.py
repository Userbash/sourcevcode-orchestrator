from __future__ import annotations

import asyncio
import logging
import os
import threading
from dataclasses import dataclass, field
from typing import Any, Optional

import uvicorn
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from starlette.concurrency import run_in_threadpool
from pydantic import BaseModel, Field

from .kernel_protocol import KernelAPI, KernelModule
from .task_submission_api import create_standard_task, normalize_user_payload
from .dev_toolkit_module import DevToolkitModule, DevToolkitRequest

logger = logging.getLogger("api_bridge_module")


class ChatRequest(BaseModel):
    user_id: str
    message: str
    session_id: str
    source: Optional[str] = None
    provider: Optional[str] = None
    trace: bool = False


class RegistrationRequest(BaseModel):
    provider_id: str
    callback_url: Optional[str] = None
    session_id: Optional[str] = None


class SourceCraftDelegateRequest(BaseModel):
    description: str
    task_type: str = "code"
    priority: str = "normal"
    repo_path: str = "."
    branch: str = "main"
    files: list[str] = Field(default_factory=list)
    constraints: list[str] = Field(default_factory=list)
    acceptance_criteria: list[str] = Field(default_factory=list)
    required_capability: str | None = None


@dataclass
class APIBridgeModule:
    name: str = "api_bridge"
    host: str = "0.0.0.0"
    port: int = 8000
    _api: KernelAPI | None = None
    _server_thread: threading.Thread | None = None
    _stop_event: asyncio.Event = field(default_factory=asyncio.Event)

    def on_load(self, api: KernelAPI) -> None:
        self._api = api
        enabled = os.getenv("AI_BRIDGE_API_ENABLED", "1").strip().lower() not in {"0", "false", "no", "off"}
        self.host = os.getenv("AI_BRIDGE_API_HOST", self.host)
        try:
            self.port = int(os.getenv("AI_BRIDGE_API_PORT", str(self.port)))
        except ValueError:
            self.port = 8000

        self._api.log("info", f"[API] {self.name} module loading...")
        if not enabled:
            self._api.log("info", "[API] api_bridge disabled by AI_BRIDGE_API_ENABLED")
            return
        if os.getenv("TESTING") == "true" or os.getenv("PYTEST_CURRENT_TEST"):
            self._api.log("info", "[API] api_bridge server start skipped in test runtime")
            return

        # We run the FastAPI server in a separate thread to not block the Orchestrator
        self._server_thread = threading.Thread(target=self._run_server, daemon=True)
        self._server_thread.start()
        self._api.log("info", f"[API] {self.name} server started on {self.host}:{self.port}")

    def _sourcecraft_module(self):
        if not self._api:
            return None
        try:
            return self._api.get_module("sourcecraft")
        except Exception:
            return None

    def _dev_toolkit_module(self):
        if not self._api:
            return None
        try:
            module = self._api.get_module("dev_toolkit")
        except Exception:
            return None
        return module if isinstance(module, DevToolkitModule) else None

    def _sourcecraft_snapshot(self) -> dict[str, Any]:
        module = self._sourcecraft_module()
        if module and hasattr(module, "finalize"):
            return module.finalize()
        return {"status": "inactive", "role": {}, "use_cases": [], "delegation_matrix": []}

    def _sourcecraft_matrix(self) -> list[dict[str, Any]]:
        snapshot = self._sourcecraft_snapshot()
        matrix = snapshot.get("delegation_matrix") or []
        return matrix if isinstance(matrix, list) else []

    def _tdd_snapshot(self) -> dict[str, Any]:
        if not self._api:
            return {"status": "inactive", "enforcement": "unknown"}
        try:
            module = self._api.get_module("tdd_policy")
        except Exception:
            module = None
        if module and hasattr(module, "finalize"):
            snapshot = module.finalize()
            if isinstance(snapshot, dict):
                return snapshot
        return {"status": "inactive", "enforcement": "unknown"}

    def _delivery_trace(self, *, source_label: str, provider_label: str, transport: str, endpoint: str) -> dict[str, Any]:
        return {
            "source": source_label,
            "provider": provider_label,
            "transport": transport,
            "endpoint": endpoint,
            "orchestrator": "submit_user_task",
            "visibility": "full",
        }

    def _health_full_snapshot(self) -> dict[str, Any]:
        if not self._api:
            return {"status": "error", "message": "Kernel API not available"}

        healthcheck = self._api.get_context("healthcheck")
        registry = self._api.get_context("registry")
        module_manager = self._api.get_context("module_manager")

        provider_health: list[dict[str, Any]] = []
        agent_health: list[dict[str, Any]] = []
        summary: dict[str, Any] = {
            "provider_count": 0,
            "agent_count": 0,
            "ready_agents": 0,
            "problem_agents": 0,
            "problem_providers": 0,
        }

        if healthcheck:
            try:
                providers = healthcheck.check_providers()
                provider_health = [item.as_dict() for item in providers.values()]
            except Exception as exc:
                provider_health = [{"provider": "unknown", "status": "error", "error": str(exc)}]
            try:
                agents = healthcheck.check_all()
                agent_health = [item.as_dict() for item in agents]
            except Exception as exc:
                agent_health = [{"agent_id": "unknown", "status": "error", "last_error": str(exc)}]

        if provider_health:
            summary["provider_count"] = len(provider_health)
            summary["problem_providers"] = sum(1 for item in provider_health if item.get("status") not in {"healthy", "degraded"})
        if agent_health:
            summary["agent_count"] = len(agent_health)
            summary["ready_agents"] = sum(1 for item in agent_health if item.get("status") == "ready")
            summary["problem_agents"] = sum(1 for item in agent_health if item.get("status") != "ready")

        module_state = module_manager.finalize() if module_manager and hasattr(module_manager, "finalize") else {}
        sourcecraft = self._sourcecraft_snapshot()
        overall_ok = bool(provider_health) and bool(agent_health) and summary["problem_agents"] == 0 and summary["problem_providers"] == 0

        return {
            "status": "ok" if overall_ok else "degraded",
            "overall_ok": overall_ok,
            "summary": summary,
            "providers": provider_health,
            "agents": agent_health,
            "modules": module_state,
            "sourcecraft": sourcecraft,
            "registry_size": len(registry.list_agents()) if registry and hasattr(registry, "list_agents") else 0,
        }

    def _build_sourcecraft_task(self, request: SourceCraftDelegateRequest):
        from .models import Priority, Task, TaskContext, TaskInput, TaskType

        task_type = request.task_type.strip().lower()
        try:
            task_enum = TaskType(task_type)
        except Exception:
            task_enum = TaskType.CODE

        priority_raw = request.priority.strip().lower()
        priority_map = {"low": Priority.LOW, "normal": Priority.NORMAL, "high": Priority.HIGH, "critical": Priority.CRITICAL}
        priority = priority_map.get(priority_raw, Priority.NORMAL)
        task = Task(
            task_enum,
            TaskInput(request.description, files=request.files, constraints=request.constraints, acceptance_criteria=request.acceptance_criteria),
            TaskContext("sourcecraft", request.repo_path, request.branch),
            priority=priority,
        )
        task.required_capability = request.required_capability or "sourcecraft"
        return task

    @staticmethod
    def _meaningful_chat_text(text: str) -> bool:
        cleaned = " ".join((text or "").split()).strip()
        if len(cleaned) < 4:
            return False
        if not any(ch.isalnum() for ch in cleaned):
            return False
        lowered = cleaned.lower()
        if lowered in {"n/a", "na", "none", "null", "undefined", "test", "asdf", "qwerty", "lol"}:
            return False
        return True

    @staticmethod
    def _unwrap_chat_payload(data: Any) -> Any:
        if isinstance(data, dict) and isinstance(data.get("c"), dict):
            return data["c"]
        return data

    @staticmethod
    def _chat_request_from_payload(data: Any) -> tuple[ChatRequest | None, list[str]]:
        if not isinstance(data, dict):
            return None, ["payload_not_object"]

        compact = {
            "user_id": data.get("user_id") or data.get("u"),
            "message": data.get("message") or data.get("m"),
            "session_id": data.get("session_id") or data.get("s"),
            "source": data.get("source") or data.get("o"),
            "provider": data.get("provider") or data.get("p"),
            "trace": bool(data.get("trace") if "trace" in data else data.get("t")),
        }

        try:
            return ChatRequest(**compact), []
        except Exception as exc:
            return None, [f"invalid_payload:{exc}"]

    def _validate_chat_request(self, request: ChatRequest) -> tuple[bool, list[str]]:
        issues: list[str] = []
        if not self._meaningful_chat_text(request.message):
            issues.append("empty_or_garbage_message")
        if not str(request.session_id or "").strip():
            issues.append("empty_session_id")
        if not str(request.user_id or "").strip():
            issues.append("empty_user_id")
        return len(issues) == 0, issues

    def _antigravity_snapshot(self) -> dict[str, Any] | None:
        if not self._api:
            return None
        module_manager = self._api.get_context("module_manager")
        if not module_manager or not hasattr(module_manager, "finalize"):
            return None
        state = module_manager.finalize().get("antigravity_status") or {}
        if isinstance(state, dict):
            snapshot = state.get("snapshot")
            if isinstance(snapshot, dict):
                return snapshot
            return state
        return None

    def _chat_ws_frames(self, request: ChatRequest, payload: dict[str, Any]) -> list[dict[str, Any]]:
        frames: list[dict[str, Any]] = []
        antigravity_status = self._antigravity_snapshot()
        if antigravity_status is not None:
            frames.append({"type": "antigravity_status", "antigravity_status": antigravity_status})
        response: dict[str, Any] = {"type": "final_result", **payload}
        if request.trace:
            response["trace"] = payload.get("trace")
        frames.append(response)
        return frames

    async def _chat_payload(self, request: ChatRequest, *, source_label: str, provider_label: str) -> dict[str, Any]:
        if not self._api:
            return {"status": "error", "message": "Kernel API not available"}

        ok, issues = self._validate_chat_request(request)
        if not ok:
            return {
                "task_id": "rejected",
                "status": "rejected",
                "source": source_label,
                "provider": provider_label,
                "issues": issues,
                "message": "invalid or empty chat payload",
            }

        raw_payload = {
            "user_id": request.user_id,
            "message": request.message,
            "session_id": request.session_id,
            "source": source_label,
            "provider": provider_label,
        }

        try:
            result = await run_in_threadpool(self._api.submit_user_task, raw_payload, source=source_label)  # type: ignore[arg-type]

            agents_used = []
            for r in result.get("results", []):
                agent_id = r.get("agent_id", "unknown")
                provider = r.get("provider") or "unknown"
                model = r.get("model") or "unknown"
                agents_used.append(f"{agent_id} [{provider} :: {model}]")

            meta_header = "\n".join([
                "╔══════════════════════════════════════════════════════════════════════╗",
                "║ 🤖 AI ORCHESTRATOR EXECUTION REPORT                                  ║",
                "╠══════════════════════════════════════════════════════════════════════╣",
                f"║ ► Tasks routed to: {', '.join(agents_used)}",
                "╚══════════════════════════════════════════════════════════════════════╝",
                ""
            ])

            merged = result.get("merged", {})
            if isinstance(merged, dict) and "summary" in merged:
                merged["summary"] = meta_header + "\n" + str(merged["summary"])
            elif isinstance(merged, str):
                merged = meta_header + "\n" + merged

            delivery = self._delivery_trace(source_label=source_label, provider_label=provider_label, transport="websocket", endpoint="/chat/ws")
            response: dict[str, Any] = {
                "task_id": result.get("task_id", "unknown"),
                "status": "completed",
                "source": source_label,
                "provider": provider_label,
                "delivery": delivery,
                "tdd": self._tdd_snapshot(),
                "result": merged if merged else result.get("results", []),
            }
            if request.trace:
                response["trace"] = {
                    "input": raw_payload,
                    "normalized": normalize_user_payload(raw_payload),
                    "raw_result": result,
                }
            return response
        except Exception as e:
            logger.exception("Error in API Bridge endpoint: %s", e)
            return {"status": "error", "message": str(e)}

    def _sourcecraft_delegate(self, request: SourceCraftDelegateRequest) -> dict[str, Any]:
        if not self._api:
            return {"status": "error", "message": "Kernel API not available"}
        task = self._build_sourcecraft_task(request)
        router = self._api.get_context("router")
        scheduler = self._api.get_context("scheduler")
        sourcecraft_module = self._sourcecraft_module()
        delegation = None
        if sourcecraft_module and hasattr(sourcecraft_module, "build_delegation_profile"):
            delegation = sourcecraft_module.build_delegation_profile(task, {
                "description": request.description,
                "repo_path": request.repo_path,
                "branch": request.branch,
                "task_type": request.task_type,
                "priority": request.priority,
            })
        route_acceptance = router.route(task) if router else None
        schedule_decision = scheduler.schedule(task) if scheduler else None
        return {
            "status": "ok",
            "sourcecraft": self._sourcecraft_snapshot(),
            "delegation": delegation,
            "task": {
                "task_id": task.task_id,
                "type": task.type.value,
                "priority": task.priority.value,
                "required_capability": task.required_capability,
                "repo_path": task.context.repo_path,
                "branch": task.context.branch,
            },
            "route": route_acceptance.as_dict() if route_acceptance else None,
            "schedule": schedule_decision.as_dict() if schedule_decision else None,
        }

    def _run_server(self) -> None:
        app = FastAPI(title="AI Orchestrator Kernel API")

        app.add_middleware(
            CORSMiddleware,
            allow_origins=["*"],
            allow_credentials=True,
            allow_methods=["*"],
            allow_headers=["*"],
        )

        @app.get("/health")
        async def health_endpoint():
            return {"status": "ok"}

        @app.get("/api/health")
        async def api_health_endpoint():
            return {"status": "ok"}

        @app.get("/sourcecraft")
        async def sourcecraft_endpoint():
            return self._sourcecraft_snapshot()

        @app.get("/sourcecraft/matrix")
        async def sourcecraft_matrix_endpoint():
            return {"status": "ok", "matrix": self._sourcecraft_matrix()}

        @app.post("/sourcecraft/delegate")
        async def sourcecraft_delegate_endpoint(request: SourceCraftDelegateRequest):
            return self._sourcecraft_delegate(request)

        @app.post("/devtoolkit/sessions")
        async def devtoolkit_create_session_endpoint(payload: dict[str, Any]):
            module = self._dev_toolkit_module()
            if not module:
                return {"status": "error", "message": "Dev Toolkit module not available"}
            session = module.load_or_create_session(
                str(payload.get("session_id") or "").strip() or None,
                mode=str(payload.get("mode") or "plan"),
                repo_context=bool(payload.get("repo_context") or False),
            )
            return {"status": "ok", "session": session.model_dump()}

        @app.get("/devtoolkit/sessions")
        async def devtoolkit_list_sessions_endpoint():
            module = self._dev_toolkit_module()
            if not module:
                return {"status": "error", "message": "Dev Toolkit module not available"}
            return {"status": "ok", "sessions": [session.model_dump() for session in module.list_sessions()]}

        @app.get("/devtoolkit/sessions/{session_id}/messages")
        async def devtoolkit_session_messages_endpoint(session_id: str):
            module = self._dev_toolkit_module()
            if not module:
                return {"status": "error", "message": "Dev Toolkit module not available"}
            return {"status": "ok", "session_id": session_id, "messages": [message.model_dump() for message in module.get_messages(session_id)]}

        @app.get("/devtoolkit/sessions/{session_id}/diff")
        async def devtoolkit_session_diff_endpoint(session_id: str):
            module = self._dev_toolkit_module()
            if not module:
                return {"status": "error", "message": "Dev Toolkit module not available"}
            return module.get_diff(session_id)

        @app.post("/devtoolkit/chat")
        async def devtoolkit_chat_endpoint(request: DevToolkitRequest):
            module = self._dev_toolkit_module()
            if not module:
                return {"status": "error", "message": "Dev Toolkit module not available"}
            result = await run_in_threadpool(module.handle_devtoolkit_chat, request)
            return result.model_dump() if hasattr(result, "model_dump") else result

        @app.post("/devtoolkit/execute")
        async def devtoolkit_execute_endpoint(request: DevToolkitRequest):
            module = self._dev_toolkit_module()
            if not module:
                return {"status": "error", "message": "Dev Toolkit module not available"}
            return module.handle_execute(request)

        @app.post("/devtoolkit/clipboard")
        async def devtoolkit_clipboard_endpoint(payload: dict[str, Any]):
            module = self._dev_toolkit_module()
            if not module:
                return {"status": "error", "message": "Dev Toolkit module not available"}
            return module.handle_clipboard(payload)

        @app.get("/health/full")
        async def health_full_endpoint():
            return self._health_full_snapshot()

        @app.get("/antigravity/status")
        async def antigravity_status_endpoint():
            module = self._api.get_module("antigravity_status") if self._api and hasattr(self._api, "get_module") else None
            if module and hasattr(module, "snapshot"):
                return {"status": "ok", "antigravity": module.snapshot()}
            return {"status": "error", "message": "Antigravity status module not available"}

        @app.post("/register_chat")
        async def register_endpoint(request: RegistrationRequest):
            if not self._api:
                return {"status": "error", "message": "Kernel API not available"}

            # Access the Chat Bus module via the Orchestrator
            bus = self._api.get_context("module_manager").get_module("chat_bus")
            if not bus:
                return {"status": "error", "message": "Chat Bus module not loaded"}

            msg = bus.register_interface(  # type: ignore
                provider_id=request.provider_id,
                callback_url=request.callback_url,
                session_id=request.session_id,
            )
            return {"status": "success", "message": msg}

        async def _chat_websocket_handler(websocket: WebSocket):
            requested_subprotocols = websocket.headers.get("sec-websocket-protocol", "").split(",")
            subprotocol = "chat.v1" if "chat.v1" in [s.strip() for s in requested_subprotocols] else "chat.json" if "chat.json" in [s.strip() for s in requested_subprotocols] else None
            await websocket.accept(subprotocol=subprotocol)
            print("[API_BRIDGE_WS] WebSocket accepted connection")
            try:
                while True:
                    data = await websocket.receive_json()
                    print(f"[API_BRIDGE_WS] Received JSON: {data}")
                    request, issues = self._chat_request_from_payload(self._unwrap_chat_payload(data))
                    if request is None:
                        await websocket.send_json({"type": "final_result", "task_id": "rejected", "status": "rejected", "issues": issues, "message": "invalid or empty chat payload"})
                        continue

                    source_label = request.source or "ws_api"
                    provider_label = request.provider or "auto"
                    payload = await self._chat_payload(request, source_label=source_label, provider_label=provider_label)
                    for frame in self._chat_ws_frames(request, payload):
                        await websocket.send_json(frame)

            except WebSocketDisconnect:
                return
            except Exception as exc:
                try:
                    await websocket.send_json({"status": "error", "message": str(exc)})
                finally:
                    return

        app.add_api_websocket_route("/chat/ws", _chat_websocket_handler)

        @app.get("/dump_memory")
        async def dump_memory_endpoint():
            if not self._api:
                return {"status": "error", "message": "Kernel API not available"}
            
            memory = self._api.get_memory()
            if not memory:
                return {"status": "error", "message": "Memory module not found"}
            
            all_keys = memory.list_keys()
            dump = {}
            for key in all_keys:
                # Key format is scope:identifier:actual_key
                parts = key.split(":")
                if len(parts) >= 3:
                    scope, identifier, k = parts[0], parts[1], ":".join(parts[2:])
                    val = memory.get(scope, identifier, k)
                    dump[key] = val
            
            return {"status": "success", "data": dump}

        @app.get("/stats")
        async def stats_endpoint():
            if not self._api:
                return {"status": "error", "message": "Kernel API not available"}
            
            # The API allows us to fetch a module directly if we have access to the orchestrator.
            # But the KernelAPI abstraction might not expose `module_manager`.
            # Let's assume we can get the orchestrator state or the module directly.
            if hasattr(self._api, "get_module"):
                usage_module = self._api.get_module("model_usage")
                if usage_module:
                    return {"status": "success", "data": usage_module.get_statistics()}
                else:
                    return {"status": "error", "message": "Module 'model_usage' is not currently loaded."}
            return {"status": "error", "message": "Cannot access module manager via API."}

        @app.post("/modules/{action}")
        async def manage_module(action: str, request: dict):
            if not self._api:
                return {"status": "error", "message": "Kernel API not available"}
            
            module_name = request.get("module_name")
            if not module_name:
                return {"status": "error", "message": "module_name is required"}
                
            if hasattr(self._api, "load_module") and hasattr(self._api, "unload_module"):
                try:
                    if action == "load":
                        self._api.load_module(module_name)
                        return {"status": "success", "message": f"Module {module_name} loaded successfully."}
                    elif action == "unload":
                        self._api.unload_module(module_name)
                        return {"status": "success", "message": f"Module {module_name} unloaded successfully."}
                    else:
                        return {"status": "error", "message": "Invalid action. Use 'load' or 'unload'."}
                except Exception as e:
                    return {"status": "error", "message": f"Operation failed: {str(e)}"}
            return {"status": "error", "message": "Kernel API does not support dynamic module loading."}

        config = uvicorn.Config(app, host=self.host, port=self.port, log_level="info", ws="wsproto")
        server = uvicorn.Server(config)
        server.run()

    def on_unload(self) -> None:
        if self._api:
            self._api.log("info", f"[API] {self.name} unloading...")
        # Uvicorn doesn't have a trivial way to stop from a thread without more ceremony
        # but since it's a daemon thread, it will exit with the process.

    def before_task(self, task: Any, context: dict[str, Any]) -> None:
        pass

    def after_task(self, task: Any, result: Any, context: dict[str, Any]) -> None:
        pass

    def finalize(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "host": self.host,
            "port": self.port,
            "status": "active" if self._server_thread and self._server_thread.is_alive() else "inactive",
        }
