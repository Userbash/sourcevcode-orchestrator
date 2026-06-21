from __future__ import annotations
import asyncio
import concurrent.futures
import hashlib
import json
import os
import threading
import time
import sys
from collections import defaultdict
from collections.abc import AsyncIterator, Callable
from pathlib import Path
from typing import Any
from datetime import UTC, datetime

from core.agents.base_agent import BaseAgent
from core.agents.codex_agent import CodexAgent

from .agent_factory import AgentFactory
from .agent_registry import AgentRegistry
from .feedback_loop import FeedbackLoop
from .healthcheck import HealthChecker
from .host_bridge import HostBridge
from .kpi import KPIEvaluator
from .load_balancer import LoadBalancer, is_agent_routable
from .metrics import MetricsCollector
from .message_bus import MessageBus
from .model_selector import ModelChoice, ModelSelector
from .models import AgentResult, AgentStatus, ExecutionPlan, P2PMessage, P2PMessageType, Priority, Task, TaskAcceptance, TaskContext, TaskInput, TaskPayload, TaskStatus, TaskType, encapsulate
from .orchestration_config import OrchestrationConfig
from .quality_analyzer import QualityAnalyzer
from .security_gate import SecurityGate
from .result_merger import ResultMerger
from .smart_scheduler import SmartScheduler
from .session_memory import MemoryScope, SessionMemory
from .memory_control_module import MemoryControlModule
from .availability import ModelAvailability, ProviderStatus
from .ai_activity_module import AIActivityModule
from .data_plane_monitor import build_data_plane_snapshot
from .antigravity_status_module import AntigravityStatusModule
from .smart_decomposer_module import SmartDecomposerModule
from .prompt_optimizer_module import PromptOptimizerModule
from .chat_bus import ChatBusModule
from .trigger_dispatcher import TriggerDispatcherModule
from .unified_vfs import UnifiedVFSModule
from .kernel_module_manager import KernelModuleManager
from .orchestrator_control_module import OrchestratorControlModule
from .qt_dev_box_module import QtDevBoxModule
from .model_usage_module import ModelUsageModule
from .local_model_manager_module import LocalModelManagerModule
from .provider_budget_router import ProviderBudgetRouter
from .provider_inventory_service import ProviderInventoryService
from .model_replacement_policy import ModelReplacementPolicy
from .cold_boot_module import ColdBootModule
from .voice_listener_module import VoiceListenerModule
from .kpi_event_logger import KPIEventLogger
from .effectiveness_dashboard import build_kpi_dashboard
from .tdd_policy_module import StrictTDDModule
from .qwen_code_module import QwenCodeModule
from .code_readability_module import CodeReadabilityModule
from .dev_toolkit_module import DevToolkitModule
from .delivery_supervisor import DeliverySupervisor
from .dependency_manager import DependencyManager
from .self_diagnostic_module import SelfDiagnosticModule
from ..mimo.proxy import MimoOrchestrationDirector
from .experience_policy_learner import ExperiencePolicyLearner
from .experience_training_pipeline import ExperienceTrainingPipeline


from .local_llm_bridge import LocalLLMBridge
from .ai_kernel_bridge import AIKernelBridge
from .local_llm_module import LocalLLMModule
from .sourcecraft_module import SourceCraftModule
from .reasoning_module import ReasoningModule
from .risk_advisor_module import RiskAdvisorModule
from .orchestrator_advisor_module import OrchestratorAdvisorModule
from .intelligence_module import AIIntelligenceModule
from .security_sentinel import KernelSecuritySentinel
from .cache_guard import CacheGuard, GuardAction
from core.adapters.state.postgres_state_store import PostgresStateStore


TIMEOUT_ERROR_TYPES = {"tcp_timeout", "api_timeout", "sdk_hang"}
from .task_decomposer import TaskDecomposer
from .task_router import CAPABILITY_BY_TASK_TYPE, TaskRouter
from .user_console import UserConsole

class OrchestratorAgent(BaseAgent):
    def __init__(self, agent_id: str = "orchestrator") -> None:
        super().__init__(agent_id, ["orchestrator", "sourcecraft", "repo_ops", "pr_flow", "release_flow", "issue_flow", "branch_governance"])
        self._provider = "local"
        self._model = "orchestrator-core"

    def run(self, task: Task, memory_context: dict | None = None) -> AgentResult:
        orchestrator = getattr(self, "orchestrator", None)
        if orchestrator is None:
            return self.result(task, "Orchestrator agent is not attached to orchestrator", TaskStatus.FAILED, errors=["orchestrator_missing"])

        sourcecraft_module = orchestrator.get_module("sourcecraft")
        if not sourcecraft_module:
            return self.result(task, "SourceCraft module is not registered", TaskStatus.FAILED, errors=["sourcecraft_module_missing"])

        repo_path = task.context.repo_path or "."
        hints = task.routing_hints if isinstance(task.routing_hints, dict) else {}
        resolution = {"action": "status"}
        if hasattr(sourcecraft_module, "resolve_repo_action_for_task"):
            resolution = sourcecraft_module.resolve_repo_action_for_task(
                task,
                {
                    "description": task.input.description,
                    "repo_path": repo_path,
                    "branch": task.context.branch,
                },
            )
        action = str(resolution.get("action") or "status")

        try:
            # Execute repo action using the module
            result_dict = sourcecraft_module.execute_repo_action(
                action=action,
                repo_path=repo_path,
                branch=resolution.get("branch"),
                target_branch=resolution.get("target_branch") or hints.get("target_branch"),
                repo_slug=resolution.get("repo_slug") or hints.get("repo_slug"),
                title=resolution.get("title") or hints.get("title"),
                description=resolution.get("description") or hints.get("description"),
                pr_slug=resolution.get("pr_slug") or hints.get("pr_slug"),
                reviewers=resolution.get("reviewers") or hints.get("reviewers"),
                session_id=task.session_id or task.task_id,
                dry_run=True, # Safety default
            )
            
            summary = result_dict.get("summary") or result_dict.get("stdout") or "SourceCraft action completed successfully."
            status = TaskStatus.DONE if result_dict.get("status") != "error" else TaskStatus.FAILED
            errors = [result_dict.get("last_error")] if result_dict.get("status") == "error" else []
            
            return self.result(
                task,
                summary,
                status=status,
                errors=errors,
                output=result_dict
            )
        except Exception as e:
            return self.result(
                task,
                f"SourceCraft action failed: {str(e)}",
                status=TaskStatus.FAILED,
                errors=[str(e)]
            )


class Orchestrator:
    def get_context(self, key: str) -> Any:
        return getattr(self, key, None)

    def emit_event(self, event_name: str, payload: dict[str, Any]) -> None:
        self.console.emit(event_name, str(payload))

    def module_state(self) -> dict[str, Any]:
        state = self.module_manager.finalize()
        state["model_availability"] = self._model_availability_state()
        state["provider_inventory"] = self._provider_inventory_snapshot if isinstance(self._provider_inventory_snapshot, dict) else {"updated_at": None, "providers": {}}
        return state

    def query_state(self, module_name: str, key: str) -> Any:
        return self.module_state().get(module_name, {}).get(key)

    def query_module_state(self, module_name: str, key: str) -> Any:
        return self.query_state(module_name, key)

    def get_memory(self) -> SessionMemory:
        return self.session_memory

    def dispatch_envelope(self, envelope):
        return self.delivery_supervisor.dispatch(envelope)

    def refresh_delivery(self, task_id: str) -> dict[str, Any]:
        return self.delivery_supervisor.refresh(task_id)

    def inspect_delivery_timeouts(self) -> dict[str, int]:
        return self.delivery_supervisor.inspect_timeouts()

    def delivery_health_snapshot(self) -> dict[str, Any]:
        return self.delivery_supervisor.delivery_health_snapshot()

    def ack_delivery(self, task_id: str, status: Any, received_by: str, reason: str | None = None):
        ack = self.message_bus.ack(task_id, status=status, received_by=received_by, reason=reason)
        try:
            self.delivery_supervisor.refresh(task_id)
        except KeyError:
            pass
        return ack

    def fetch_agent_mailbox(self, agent_id: str, *, limit: int = 1):
        return self.delivery_supervisor.fetch_agent_mailbox(agent_id, limit=limit)

    def confirm_delivery_payload(self, task_id: str, agent_id: str, envelope) -> bool:
        return self.delivery_supervisor.confirm_payload(task_id, agent_id, envelope)

    def establish_delivery_handshake(self, task_id: str, agent_id: str):
        return self.delivery_supervisor.establish_delivery(task_id, agent_id)

    def mailbox_snapshot(self, agent_id: str) -> dict[str, Any]:
        return self.delivery_supervisor.mailbox_snapshot(agent_id)

    def _delivery_envelope_for_task(self, task: Task, agent_id: str, capability: str):
        payload = TaskPayload(
            objective=task.input.description,
            input_data={"files": list(task.input.files), "constraints": list(task.input.constraints)},
            context={
                "project": task.context.project,
                "repo_path": task.context.repo_path,
                "branch": task.context.branch,
                "task_id": task.task_id,
                "task_type": task.type.value,
            },
            acceptance_criteria=list(task.input.acceptance_criteria),
            expected_output_format=task.expected_output or "agent_result",
            artifacts=list(task.input.files),
        )
        return encapsulate(
            payload,
            {
                "task_id": task.task_id,
                "trace_id": task.task_id,
                "source_agent": "orchestrator",
                "target_agent": agent_id,
                "target_capability": capability,
                "priority": task.priority.value,
                "max_hops": 5,
                "max_retries": max(1, int(task.retry_count) + 1),
            },
        )

    def _ensure_agent_worker(self, agent_id: str) -> None:
        thread = self._agent_worker_threads.get(agent_id)
        if thread is not None and thread.is_alive():
            return
        worker = threading.Thread(target=self._agent_worker_loop, args=(agent_id,), name=f"agent-worker-{agent_id}", daemon=True)
        self._agent_worker_threads[agent_id] = worker
        worker.start()

    def _agent_worker_loop(self, agent_id: str) -> None:
        while not self._agent_worker_stop.is_set():
            try:
                message = self.message_bus.receive_for_agent(agent_id)
            except Exception:
                time.sleep(0.05)
                continue
            if message is None:
                time.sleep(0.05)
                continue
            if isinstance(message, P2PMessage):
                with self._agent_runtime_lock:
                    self._agent_p2p_inbox[agent_id].append(message)
                continue
            if not hasattr(message, "task_id"):
                continue
            task_id = str(getattr(message, "task_id", "") or "")
            with self._agent_runtime_lock:
                runtime = self._agent_task_runtime.get(task_id)
                future = self._agent_task_futures.get(task_id)
            if runtime is None or future is None:
                continue
            task = runtime["task"]
            memory_context = dict(runtime.get("memory_context") or {})
            agent = self.local_agents.get(agent_id)
            if agent is None:
                if not future.done():
                    future.set_result(AgentResult(task.task_id, agent_id, TaskStatus.FAILED, {"summary": "No local executor for routed agent", "files_changed": [], "commands_run": [], "test_results": [], "diff": ""}, 0.0, ["No local executor"], []))
                continue
            if not self.confirm_delivery_payload(task_id, agent_id, message):
                if not future.done():
                    future.set_result(AgentResult(task.task_id, agent_id, TaskStatus.FAILED, {"summary": "Delivery payload validation failed", "files_changed": [], "commands_run": [], "test_results": [], "diff": ""}, 0.0, ["delivery_payload_invalid"], []))
                continue
            ack = self.establish_delivery_handshake(task_id, agent_id)
            if ack.ack_status.value != "accepted":
                if not future.done():
                    future.set_result(AgentResult(task.task_id, agent_id, TaskStatus.FAILED, {"summary": "Delivery handshake was not accepted", "files_changed": [], "commands_run": [], "test_results": [], "diff": ""}, 0.0, ["delivery_handshake_failed"], []))
                continue
            handoff = self._consume_p2p_handoffs(agent_id, task_id)
            if handoff:
                memory_context["p2p_handoffs"] = handoff
                memory_context["handoff_summaries"] = [item.get("summary", "") for item in handoff if item.get("summary")]
            try:
                result = agent.run(task, memory_context=memory_context)
            except Exception as exc:
                result = AgentResult(task.task_id, agent_id, TaskStatus.FAILED, {"summary": str(exc), "files_changed": [], "commands_run": [], "test_results": [], "diff": ""}, 0.0, [str(exc)], [])
            if not future.done():
                future.set_result(result)

    def _consume_p2p_handoffs(self, agent_id: str, task_id: str) -> list[dict[str, Any]]:
        with self._agent_runtime_lock:
            inbox = list(self._agent_p2p_inbox.get(agent_id, []))
            matched = [msg for msg in inbox if msg.task_id == task_id]
            remaining = [msg for msg in inbox if msg.task_id != task_id]
            self._agent_p2p_inbox[agent_id] = remaining
        payloads: list[dict[str, Any]] = []
        for message in matched:
            payload = dict(message.payload or {})
            payload.setdefault("from_agent", message.from_agent)
            payload.setdefault("to_agent", message.to_agent)
            payload.setdefault("message_type", str(message.message_type.value if hasattr(message.message_type, "value") else message.message_type))
            payloads.append(payload)
        return payloads

    def _dispatch_dependency_handoffs(self, tasks: list[Task], results_by_task_id: dict[str, AgentResult]) -> int:
        dispatched = 0
        for task in tasks:
            target_agent = self._task_preferred_agent_id(task)
            if not target_agent or not task.dependencies:
                continue
            for dep_id in task.dependencies:
                result = results_by_task_id.get(dep_id)
                if result is None or not result.agent_id or result.agent_id == target_agent:
                    continue
                payload = {
                    "dependency_task_id": dep_id,
                    "summary": result.output.get("summary", ""),
                    "errors": list(result.errors or []),
                    "status": result.status.value,
                    "artifacts": list(result.output.get("files_changed", [])),
                }
                message = P2PMessage(task_id=task.task_id, from_agent=result.agent_id, to_agent=target_agent, message_type=P2PMessageType.CONTEXT_TRANSFER, priority=task.priority, payload=payload)
                peer_candidates = [peer for peer in self.message_bus.discover_peers("review") if peer not in {result.agent_id, target_agent}] if hasattr(self.message_bus, "discover_peers") else []
                if peer_candidates:
                    self.message_bus.relay_p2p(message, nearest_peer=peer_candidates[0])
                else:
                    self.message_bus.send_p2p(message)
                dispatched += 1
        return dispatched

    def _run_local_agent_via_delivery(self, task: Task, agent_id: str, capability: str, agent: BaseAgent, memory_context: dict[str, object]) -> AgentResult:
        self._ensure_agent_worker(agent_id)
        future: concurrent.futures.Future[AgentResult] = concurrent.futures.Future()
        with self._agent_runtime_lock:
            self._agent_task_runtime[task.task_id] = {"task": task, "memory_context": dict(memory_context), "agent_id": agent_id, "capability": capability}
            self._agent_task_futures[task.task_id] = future
        envelope = self._delivery_envelope_for_task(task, agent_id, capability)
        self.dispatch_envelope(envelope)
        try:
            return future.result(timeout=float(self._agent_worker_timeout_sec))
        except concurrent.futures.TimeoutError:
            return AgentResult(task.task_id, agent_id, TaskStatus.FAILED, {"summary": "Agent worker timed out waiting for mailbox consumer", "files_changed": [], "commands_run": [], "test_results": [], "diff": ""}, 0.0, ["delivery_worker_timeout"], [])
        finally:
            with self._agent_runtime_lock:
                self._agent_task_runtime.pop(task.task_id, None)
                self._agent_task_futures.pop(task.task_id, None)

    def log(self, level: str, message: str) -> None:
        getattr(self.console, level, self.console.emit)(f"KERNEL:{level.upper()}", message)

    def get_module(self, name: str) -> Any:
        return self.module_manager.get_module(name)

    def load_module(self, name: str) -> None:
        self.module_manager.load(name)

    def unload_module(self, name: str) -> None:
        self.module_manager.unload(name)

    @staticmethod
    def _local_llm_autostart_enabled() -> bool:
        return os.getenv("AI_BRIDGE_AUTOSTART_LOCAL_LLM", "true").strip().lower() in {"1", "true", "yes", "on"}

    
    
    @staticmethod
    def _easy_diffusion_autostart_enabled() -> bool:
        return os.getenv("AI_BRIDGE_AUTOSTART_EASY_DIFFUSION", "false").strip().lower() in {"1", "true", "yes", "on"}

    @staticmethod
    def _ai_kernel_autostart_enabled() -> bool:
        return os.getenv("AI_BRIDGE_AUTOSTART_AI_KERNEL", "true").strip().lower() in {"1", "true", "yes", "on"}

    @staticmethod
    def _testing_mode() -> bool:
        return os.getenv("TESTING", "").strip().lower() == "true" or bool(os.getenv("PYTEST_CURRENT_TEST"))

    def _antigravity_status_snapshot(self) -> dict[str, Any]:
        if self._testing_mode():
            return {}
        module = self.module_manager.get_module("antigravity_status")
        if module and hasattr(module, "snapshot"):
            snapshot = module.snapshot()
            return snapshot if isinstance(snapshot, dict) else {"value": snapshot}
        return {}

    def _provider_health_snapshot(self) -> dict[str, Any]:
        providers: dict[str, Any] = {}
        try:
            cached = self.availability.cached_report() if hasattr(self.availability, "cached_report") else {}
            if isinstance(cached, dict):
                for provider, state in cached.items():
                    if isinstance(state, dict):
                        providers[str(provider).strip().lower()] = dict(state)
        except Exception:
            pass
        antigravity = self._antigravity_status_snapshot()
        if isinstance(antigravity, dict) and antigravity:
            providers["antigravity"] = antigravity
        mimo_director = getattr(self, "mimo_director", None)
        if mimo_director is not None and hasattr(mimo_director, "status_snapshot"):
            try:
                mimo = mimo_director.status_snapshot()
                if isinstance(mimo, dict) and mimo:
                    providers["mimo"] = mimo
            except Exception:
                pass
        return {"providers": providers}

    def _model_availability_state(self) -> dict[str, Any]:
        cached = self.availability.cached_report() if hasattr(self.availability, "cached_report") else {}
        cached_report = cached if isinstance(cached, dict) else {}
        provider_status = {
            provider: state.get("status")
            for provider, state in cached_report.items()
            if isinstance(state, dict)
        }
        return {
            "status": "active",
            "provider_count": len(cached_report),
            "providers": provider_status,
            "cached_report": cached_report,
            "source": "orchestrator.availability",
        }

    def cache_guard_snapshot(self, session_id: str) -> dict[str, Any]:
        return self.cache_guard.snapshot(session_id)

    def _cache_guard_failure(self, task: Task) -> AgentResult | None:
        session_id = task.session_id or task.task_id
        guard_snapshot = self.cache_guard.snapshot(session_id)
        if not guard_snapshot.get("blocked"):
            return None
        message = f"Task blocked by cache guard for session {session_id}"
        self.state_store.record_invalidation(
            session_id,
            reason="CACHE_GUARD_HARD_STOP",
            payload={"task_id": task.task_id, "action": guard_snapshot.get("action")},
        )
        return AgentResult(
            task.task_id,
            "orchestrator",
            TaskStatus.FAILED,
            {"summary": message, "files_changed": [], "commands_run": [], "test_results": [], "diff": ""},
            0.0,
            [message],
            [],
        )

    @staticmethod
    def _runtime_usage_hints(task: Task) -> dict[str, Any]:
        hints = getattr(task, "routing_hints", {}) or {}
        runtime = hints.get("runtime_usage") if isinstance(hints, dict) else {}
        return dict(runtime) if isinstance(runtime, dict) else {}

    @staticmethod
    def _default_context_version(task: Task) -> str:
        return f"task:{task.task_id}"

    def _select_model_choice_with_mimo(self, task: Task, advisory_context: dict[str, Any], current_budget: float, memory_context: dict[str, Any] | None = None) -> tuple[ModelChoice | None, Any]:
        complexity = self.model_selector.classify(task)
        task.complexity = complexity
        recommendation = self.mimo_director.recommend_model(task, advisory_context, current_budget=current_budget, memory_context=memory_context)
        if not recommendation.allow:
            return None, recommendation

        if recommendation.decision_mode in {"safe_fallback", "surrogate_controller"}:
            choice = ModelChoice(
                recommendation.model_name,
                recommendation.provider,
                complexity,
                requires_secondary_review=task.type == TaskType.PLAN,
                reason=recommendation.reason,
            )
            choice.selection_trace = {
                "provider": choice.provider,
                "model_name": choice.model_name,
                "complexity": getattr(choice.complexity, "value", str(choice.complexity)),
                "reason": choice.reason,
                "task_type": getattr(task.type, "value", str(task.type)),
                "mimo_decision_mode": recommendation.decision_mode,
                "mimo_recommendation": {
                    "provider": recommendation.provider,
                    "model_name": recommendation.model_name,
                    "confidence": recommendation.confidence,
                },
            }
            return choice, recommendation

        choice = self.model_selector.select(task, advisory_context=advisory_context)
        choice.provider = recommendation.provider
        choice.model_name = recommendation.model_name
        choice.reason = recommendation.reason
        trace = choice.selection_trace if isinstance(choice.selection_trace, dict) else {}
        trace["mimo_decision_mode"] = recommendation.decision_mode
        trace["mimo_recommendation"] = {
            "provider": recommendation.provider,
            "model_name": recommendation.model_name,
            "confidence": recommendation.confidence,
        }
        choice.selection_trace = trace
        return choice, recommendation

    def _autostart_local_llm(self) -> None:
        if self._testing_mode() or not self._local_llm_autostart_enabled():
            return

        module = self.module_manager.get_module("local_llm")
        if not isinstance(module, LocalLLMModule):
            self.log("warning", "[LOCAL_LLM] local_llm module is not registered; skipping autostart.")
            return

        try:
            ready = self.local_llm_bridge.ensure_ready(module.model_name)
        except Exception as exc:
            self.log("warning", f"[LOCAL_LLM] Autostart failed: {exc}")
            return

        if ready:
            self.log("info", f"[LOCAL_LLM] Autostart complete for {module.model_name}.")
        else:
            self.log("warning", f"[LOCAL_LLM] Autostart could not confirm readiness for {module.model_name}.")

    def _autostart_ai_kernel(self) -> None:
        if self._testing_mode() or not self._ai_kernel_autostart_enabled():
            return

        if not os.getenv("AI_KERNEL_ENABLED", "true").strip().lower() in {"1", "true", "yes", "on"}:
            return

        model_name = (os.getenv("AI_KERNEL_MODEL_ALIAS") or "hauhaucs-qwen36-35b-a3b-aggressive:q4_k_m").strip()
        try:
            ready = self.ai_kernel_bridge.ensure_ready(model_name)
        except Exception as exc:
            self.log("warning", f"[AI_KERNEL] Autostart failed: {exc}")
            return

        if ready:
            self.log("info", f"[AI_KERNEL] Autostart complete for {model_name}.")
        else:
            self.log("warning", f"[AI_KERNEL] Autostart could not confirm readiness for {model_name}.")

    def _autostart_easy_diffusion(self) -> None:
        if self._testing_mode() or not self._easy_diffusion_autostart_enabled():
            return

        module = self.module_manager.get_module("easy_diffusion")
        manager = getattr(module, "manager", None)
        if manager is None:
            self.log("warning", "[EASY_DIFFUSION] easy_diffusion module is not registered; skipping autostart.")
            return

        try:
            ready = manager.boot_autostart()
        except Exception as exc:
            self.log("warning", f"[EASY_DIFFUSION] Autostart failed: {exc}")
            return

        if ready:
            self.log("info", "[EASY_DIFFUSION] Autostart complete.")
        else:
            self.log("warning", "[EASY_DIFFUSION] Autostart could not confirm readiness.")

    def __init__(self, registry: AgentRegistry | None = None, retry_limit: int = 3, idle_shutdown_sec: int = 900, verbose_orchestrator: bool = False, json_console: bool = False) -> None:
        self.local_agents: dict[str, BaseAgent] = {}
        self.results: dict[str, AgentResult] = {}
        self.live_trace_rows: list[dict[str, object]] = []
        self._agent_worker_stop = threading.Event()
        self._agent_worker_threads: dict[str, threading.Thread] = {}
        self._agent_task_futures: dict[str, concurrent.futures.Future[AgentResult]] = {}
        self._agent_task_runtime: dict[str, dict[str, Any]] = {}
        self._agent_runtime_lock = threading.Lock()
        self._agent_p2p_inbox: dict[str, list[P2PMessage]] = defaultdict(list)
        self._agent_worker_timeout_sec = max(30, int(os.getenv("AI_BRIDGE_AGENT_WORKER_TIMEOUT_SEC", "900") or "900"))
        
        components = AgentFactory.build(registry=registry, retry_limit=retry_limit, idle_shutdown_sec=idle_shutdown_sec)
        
        self.registry = components.registry
        self.lifecycle = components.lifecycle
        self.autoscaler = components.autoscaler
        self.load_balancer = components.load_balancer
        self.model_selector = components.model_selector
        self.decomposer = components.decomposer
        self.router = components.router
        self.orchestration_config = components.orchestration_config
        self.scheduler = components.scheduler
        self.message_bus = components.message_bus
        self.healthcheck = components.healthcheck
        self.healthcheck.set_module_state_source(self.module_state)
        self.availability = ModelAvailability()
        self.provider_inventory = ProviderInventoryService()
        self.model_replacement_policy = ModelReplacementPolicy()
        self._provider_inventory_snapshot: dict[str, Any] = self.provider_inventory.read_snapshot()
        self.feedback = components.feedback
        self.metrics = components.metrics
        self.kpi = components.kpi
        self.kpi.task_thresholds = dict(self.orchestration_config.kpi_thresholds_by_task)
        self.quality = components.quality
        self.merger = components.merger
        self.console = components.console
        log_file = os.getenv("ORCHESTRATOR_LOG_FILE") or os.getenv("ORCHESTRATOR_JSONL_LOG")
        env_json = os.getenv("ORCHESTRATOR_JSON_CONSOLE", "").strip().lower() in {"1", "true", "yes", "on"}
        env_color = os.getenv("ORCHESTRATOR_COLOR_LOGS", "").strip().lower()
        color_mode = env_color in {"1", "true", "yes", "on"} if env_color else (sys.stdout.isatty() and not json_console)
        self.console.set_mode(
            json_mode=json_console or env_json,
            verbose=verbose_orchestrator,
            color_mode=color_mode,
            log_path=log_file,
        )
        self.verbose_orchestrator = verbose_orchestrator
        self.json_console = json_console or env_json
        self.console.emit("START", f"Orchestrator ready | verbose={self.verbose_orchestrator} | json={self.json_console} | color={color_mode} | log_file={log_file or 'off'}")
        self.security_gate = components.security_gate
        self.host_bridge = components.host_bridge
        self.session_memory = components.session_memory
        self.memory_consolidator = components.memory_consolidator
        self.layered_context_memory = self.session_memory.layered
        self.provider_budget_router = ProviderBudgetRouter()
        self.state_store = PostgresStateStore()
        self.cache_guard = CacheGuard()
        self.kpi_events = KPIEventLogger.from_env()
        if getattr(self.orchestration_config, "kpi_rejection_summary_path", ""):
            self.kpi_events.summary_path = Path(self.orchestration_config.kpi_rejection_summary_path)
        self.delivery_supervisor = DeliverySupervisor(
            self.message_bus,
            session_memory=self.session_memory,
            kpi_events=self.kpi_events,
            ack_timeout_sec=max(5, int(os.getenv("AI_BRIDGE_DELIVERY_ACK_TIMEOUT_SEC", "30") or "30")),
        )
        self._postgres_watchdog_stop = threading.Event()
        self._postgres_watchdog_thread: threading.Thread | None = None
        self._training_consolidation_stop = threading.Event()
        self._kpi_dashboard_stop = threading.Event()
        self._training_consolidation_lock = threading.Lock()
        self._training_consolidation_queue: list[dict[str, Any]] = []
        self._training_consolidation_task: asyncio.Task[None] | None = None
        self._kpi_dashboard_task: asyncio.Task[None] | None = None
        self._provider_inventory_task: asyncio.Task[None] | None = None
        self._provider_inventory_stop = threading.Event()
        self._training_consolidation_interval_sec = max(60, int(self.orchestration_config.training_consolidation_interval_sec))
        self._kpi_dashboard_interval_sec = max(300, int(getattr(self.orchestration_config, "kpi_dashboard_interval_sec", 3600)))
        self._provider_inventory_refresh_interval_sec = max(60, int(getattr(self.orchestration_config, "provider_inventory_refresh_interval_sec", 1800)))
        self._openai_template_agent_ids: set[str] = set()
        self.local_llm_bridge = LocalLLMBridge(host_bridge=self.host_bridge)
        self.ai_kernel_bridge = AIKernelBridge(host_bridge=self.host_bridge)
        self.mimo_director = MimoOrchestrationDirector()
        self.experience_policy_learner = ExperiencePolicyLearner()
        self.experience_trainer = ExperienceTrainingPipeline()
        self.mimo_director.set_memory_source(self.session_memory)
        self.mimo_director.set_history_source(self.session_memory)
        self.mimo_director.set_kpi_source(self.kpi)
        self.mimo_director.set_quality_source(self.quality)

        # Connect API for smart modules
        self.model_selector.set_api(self)
        self.router.set_api(self)
        
        self.module_manager = KernelModuleManager()
        self.module_manager.set_api(self)
        self.module_manager.register(AIActivityModule())
        self.module_manager.register(OrchestratorControlModule())
        self.module_manager.register(MemoryControlModule())
        self.module_manager.register(ModelUsageModule())
        self.module_manager.register(LocalModelManagerModule())
        self.module_manager.register(AntigravityStatusModule())
        self.mimo_director.set_status_source(self._provider_health_snapshot)
        self.module_manager.register(SmartDecomposerModule())
        self.module_manager.register(PromptOptimizerModule())
        self.module_manager.register(ChatBusModule())
        self.module_manager.register(TriggerDispatcherModule())
        self.module_manager.register(UnifiedVFSModule())
        self.module_manager.register(ColdBootModule())
        self.module_manager.register(QtDevBoxModule())
        self.module_manager.register(StrictTDDModule())
        self.module_manager.register(QwenCodeModule())
        self.module_manager.register(CodeReadabilityModule())
        self.module_manager.register(DevToolkitModule())
        self.module_manager.register(SelfDiagnosticModule())



        self.module_manager.register(LocalLLMModule())
        self.module_manager.register(SourceCraftModule())
        self.module_manager.register(VoiceListenerModule())
        self.module_manager.register(ReasoningModule())
        self.module_manager.register(RiskAdvisorModule())
        self.module_manager.register(OrchestratorAdvisorModule())
        self.module_manager.register(AIIntelligenceModule())
        self.module_manager.register(KernelSecuritySentinel())
        
        self.module_manager.load("ai_activity")
        self.module_manager.load("orchestrator_control")
        self.module_manager.load("memory_control")
        self.module_manager.load("model_usage")
        self.module_manager.load("local_model_manager")
        self.mimo_director.set_budget_module(self.module_manager.get_module("model_usage"))
        self.module_manager.load("unified_vfs")
        self.mimo_director.set_vfs_source(self.module_manager.get_module("unified_vfs"))
        self.mimo_director.safe_sync()
        try:
            self._refresh_provider_inventory_snapshot(force_refresh=False)
        except Exception as exc:
            self.console.emit("INVENTORY", f"initial provider inventory refresh failed: {exc}")
        self.module_manager.load("smart_decomposer")
        self.module_manager.load("prompt_optimizer")
        self.module_manager.load("chat_bus")
        self.module_manager.load("trigger_dispatcher")
        self.module_manager.load("cold_boot")
        self.module_manager.load("tdd_policy")
        self.module_manager.load("qwen_code")
        self.module_manager.load("readability_policy")
        self.module_manager.load("dev_toolkit")
        self.module_manager.load("self_diagnostic")



        sourcecraft_disabled = os.getenv("AI_BRIDGE_DISABLE_SOURCECRAFT", "false").strip().lower() in {"1", "true", "yes", "on"}
        if sourcecraft_disabled:
            self.console.emit("SOURCECRAFT", "disabled by AI_BRIDGE_DISABLE_SOURCECRAFT")
        else:
            try:
                self.module_manager.load("sourcecraft")
            except Exception as exc:
                self.console.emit("SOURCECRAFT", f"degraded at boot: {exc}")
        if not self._testing_mode():
            self.module_manager.load("voice_listener")
            self.module_manager.load("reasoning")
        self.module_manager.load("risk_advisor")
        self.module_manager.load("orchestrator_advisor")
        self.module_manager.load("intelligence")
        self.module_manager.load("security_sentinel")
        self.experience_policy_learner.refresh(persistent=self.session_memory.hybrid.persistent)
        self.experience_trainer.train(
            persistent=self.session_memory.hybrid.persistent,
            runtime_snapshot=self._training_runtime_snapshot(),
            repo_path=os.getcwd(),
        )

        # Load local_llm before autostart so the module is available for
        # advisory context and readiness checks during kernel boot.
        if not self._testing_mode():
            self.module_manager.load("local_llm")
        self._autostart_local_llm()
        self._autostart_ai_kernel()
        self._autostart_easy_diffusion()
        # Register default orchestrator agent to handle sourcecraft and orchestrator capability tasks
        self.attach_local_agent("orchestrator", OrchestratorAgent("orchestrator"), agent_type="orchestrator", critical=True, model_name="orchestrator-core", provider="local")
        if not self._testing_mode():
            self._start_postgres_watchdog()

    def _start_postgres_watchdog(self) -> None:
        interval_raw = os.getenv("AI_BRIDGE_POSTGRES_WATCHDOG_INTERVAL_SEC", "30").strip()
        try:
            interval = max(5, int(interval_raw))
        except ValueError:
            interval = 30
        if os.getenv("AI_BRIDGE_POSTGRES_WATCHDOG_ENABLED", "1").strip().lower() in {"0", "false", "no", "off"}:
            return
        if self._postgres_watchdog_thread and self._postgres_watchdog_thread.is_alive():
            return

        def _watchdog() -> None:
            while not self._postgres_watchdog_stop.is_set():
                try:
                    snapshot = build_data_plane_snapshot(
                        database_url=os.getenv("AI_BRIDGE_MEMORY_DATABASE_URL", "").strip(),
                        rabbitmq_url=os.getenv("AI_BRIDGE_RABBITMQ_URL", "").strip(),
                    )
                    payload = {
                        "type": "postgres_watchdog",
                        "status": snapshot.postgres_state,
                        "ok": snapshot.ok,
                        "details": snapshot.details,
                        "probe": snapshot.probe,
                        "tables": snapshot.as_dict().get("tables", []),
                    }
                    self.kpi_events.write(payload)
                    if not snapshot.ok and self.console:
                        self.console.emit("POSTGRES_ALERT", f"state={snapshot.postgres_state} details={snapshot.details}")
                except Exception as exc:
                    try:
                        self.kpi_events.write({"type": "postgres_watchdog", "status": "error", "error": str(exc)})
                    except Exception:
                        pass
                    if self.console:
                        self.console.emit("POSTGRES_ALERT", f"watchdog_error={exc}")
                self._postgres_watchdog_stop.wait(interval)

        self._postgres_watchdog_thread = threading.Thread(target=_watchdog, name="postgres-watchdog", daemon=True)
        self._postgres_watchdog_thread.start()

    def _stop_postgres_watchdog(self) -> None:
        self._postgres_watchdog_stop.set()

    def _training_runtime_snapshot(self) -> dict[str, Any]:
        local_llm = self.get_module("local_llm") if hasattr(self, "get_module") else None
        provider_inventory = getattr(self, "_provider_inventory_snapshot", {})
        providers = provider_inventory.get("providers", {}) if isinstance(provider_inventory, dict) else {}
        return {
            "local_llm_ready": bool(local_llm and getattr(local_llm, "ready", False)),
            "local_llm_model": str(getattr(local_llm, "model_name", "") or ""),
            "ai_kernel_enabled": os.getenv("AI_KERNEL_ENABLED", "true").strip().lower() in {"1", "true", "yes", "on"},
            "provider_inventory_ready": bool(providers),
        }

    def _training_memory_domain(self, task: Task) -> str:
        task_type = task.type.value.lower()
        return {
            "plan": "prompt:plan",
            "review": "prompt:review",
            "test": "prompt:test",
            "code": "prompt:code",
            "docs": "prompt:docs",
            "research": "prompt:research",
        }.get(task_type, f"prompt:{task_type}")

    def _enqueue_training_consolidation(self, task: Task, result: AgentResult) -> None:
        memory_domain = self._training_memory_domain(task)
        model_name = str(result.model_name or getattr(task, "assigned_model", "") or "").strip()
        provider = str(result.provider or "").strip().lower()
        files = [str(item).strip() for item in list(task.input.files or []) if str(item).strip()]
        constraints = [str(item).strip() for item in list(task.input.constraints or []) if str(item).strip()]
        acceptance = [str(item).strip() for item in list(task.input.acceptance_criteria or []) if str(item).strip()]
        description = str(task.input.description or '').strip()
        summary = str(result.output.get("summary", "") or "").strip()
        payload = {
            "session_id": task.session_id or task.task_id,
            "agent_id": result.agent_id or "orchestrator",
            "task_type": task.type.value,
            "memory_domain": memory_domain,
            "summary": summary,
            "problem": description,
            "objective": description,
            "constraints": constraints,
            "files": files,
            "acceptance_criteria": acceptance,
            "outcome": result.status.value,
            "failure_mode": "; ".join(result.errors or []) if result.errors else "",
            "reuse_hint": "Reuse when task type, files, and constraints overlap; treat as a pattern rather than a literal script.",
            "source_memory_ids": [],
            "quality_score": max(0.0, min(1.0, float(result.confidence))),
            "metadata": {
                "task_id": task.task_id,
                "status": result.status.value,
                "memory_domain": memory_domain,
                "model_name": model_name,
                "provider": provider,
                "summary": summary,
                "task_description": description,
                "constraints": constraints,
                "files": files,
                "acceptance_criteria": acceptance,
                "failure_mode": "; ".join(result.errors or []) if result.errors else "",
                "reuse_hint": "Reuse when task type, files, and constraints overlap; treat as a pattern rather than a literal script.",
                "outcome": result.status.value,
                "problem": description,
            },
        }
        if not payload["summary"]:
            payload["summary"] = f"Successful {task.type.value} task {task.task_id}"
        with self._training_consolidation_lock:
            self._training_consolidation_queue.append(payload)
        if self._training_consolidation_task is None or self._training_consolidation_task.done():
            self._flush_training_consolidation_queue()

    def _flush_training_consolidation_queue(self) -> int:
        drained: list[dict[str, Any]] = []
        with self._training_consolidation_lock:
            if self._training_consolidation_queue:
                drained = self._training_consolidation_queue[:]
                self._training_consolidation_queue.clear()
        if not drained:
            return 0

        processed = 0
        for item in drained:
            try:
                self.memory_consolidator.consolidate_successful_task(
                    session_id=str(item["session_id"]),
                    agent_id=str(item["agent_id"]),
                    task_type=str(item["task_type"]),
                    summary=str(item["summary"]),
                    source_memory_ids=list(item.get("source_memory_ids") or []),
                    quality_score=float(item.get("quality_score", 0.0)),
                    metadata=dict(item.get("metadata") or {}),
                )
                processed += 1
            except Exception:
                self.log("warning", f"[MEMORY] Failed to consolidate trained memory for task_type={item.get('task_type')}")
        if processed:
            try:
                self.experience_policy_learner.refresh(persistent=self.session_memory.hybrid.persistent)
            except Exception as exc:
                self.log("warning", f"[MEMORY] Experience policy refresh failed: {exc}")
            try:
                training_refresh = self.experience_trainer.train(
                    persistent=self.session_memory.hybrid.persistent,
                    runtime_snapshot=self._training_runtime_snapshot(),
                    repo_path=os.getcwd(),
                )
                supervisor = ((training_refresh or {}).get('training_supervisor') or {}).get('primary', {})
                if supervisor:
                    self.console.emit("TRAINING", f"supervisor={supervisor.get('owner', 'orchestrator')} task_board={(training_refresh or {}).get('training_task_board_path', '')}")
            except Exception as exc:
                self.log("warning", f"[MEMORY] Experience training refresh failed: {exc}")
        return processed

    async def _training_consolidation_loop(self) -> None:
        while not self._training_consolidation_stop.is_set():
            await asyncio.sleep(self._training_consolidation_interval_sec)
            self._flush_training_consolidation_queue()

    def _update_model_replacement_snapshot(self) -> dict[str, Any]:
        participation = self._provider_inventory_snapshot.get("participation", {}) if isinstance(self._provider_inventory_snapshot, dict) else {}
        replacement = self.model_replacement_policy.build_snapshot(participation if isinstance(participation, dict) else {})
        if not isinstance(self._provider_inventory_snapshot, dict):
            self._provider_inventory_snapshot = {"updated_at": int(__import__("time").time()), "providers": {}, "participation": {}}
        self._provider_inventory_snapshot["replacement_policy"] = replacement
        return replacement

    def _refresh_provider_inventory_snapshot(self, *, force_refresh: bool = False) -> dict[str, Any]:
        payload = self.provider_inventory.refresh(force_refresh=force_refresh)
        openai_entry = payload.get("openai", {}) if isinstance(payload, dict) else {}
        primary_model = str(os.getenv("CODEX_OPENAI_MODEL", "gpt-5.5")).strip()
        if isinstance(openai_entry, dict) and openai_entry.get("ok"):
            template_sync = self.sync_openai_template_workers(enabled=True, primary_model=primary_model)
            diagnostics = openai_entry.get("diagnostics")
            if isinstance(diagnostics, dict):
                diagnostics["worker_sync"] = template_sync
        participation = self.provider_inventory.build_participation_snapshot(self.registry.list_agents())
        self._provider_inventory_snapshot = {"updated_at": int(__import__("time").time()), "providers": payload, "participation": participation}
        self._update_model_replacement_snapshot()
        return self._provider_inventory_snapshot

    def _apply_model_replacement_policy(
        self,
        task: Task,
        capability: str,
        choice: Any,
        agent_id: str,
        agent_record: Any,
        module_context: dict[str, object],
        *,
        failure_reason: str | None = None,
        exclude_agents: set[str] | None = None,
        allow_same_agent: bool = True,
    ) -> tuple[str, Any, dict[str, Any] | None]:
        participation = self._provider_inventory_snapshot.get("participation", {}) if isinstance(self._provider_inventory_snapshot, dict) else {}
        current_provider = self._normalize_provider(agent_record.provider if agent_record else choice.provider)
        current_model = str(getattr(task, "assigned_model", "") or (agent_record.model_name if agent_record else choice.model_name) or "").strip()
        recommendation = self.model_replacement_policy.recommend_replacement(
            task,
            current_provider,
            current_model,
            participation if isinstance(participation, dict) else {},
            failure_reason=failure_reason,
        )
        if not recommendation:
            return agent_id, agent_record, None

        target_provider = self._normalize_provider(str(recommendation.get("provider") or current_provider))
        target_model = str(recommendation.get("model_name") or "").strip()
        if not target_model:
            return agent_id, agent_record, None

        target_agent_id = agent_id
        target_agent_record = agent_record
        exclude = set(exclude_agents or set())
        if allow_same_agent and agent_id in exclude:
            exclude.remove(agent_id)
        if target_provider != current_provider:
            candidate_id = self._select_agent_by_provider_preference(capability, [target_provider], exclude=exclude, priority=task.priority)
            if not candidate_id:
                return agent_id, agent_record, None
            target_agent_id = candidate_id
            target_agent_record = self.registry.get(candidate_id)
            if target_agent_record is None:
                return agent_id, agent_record, None

        task.assigned_model = target_model
        module_context["assigned_model"] = target_model
        module_context["replacement_policy"] = recommendation
        module_context["model"] = target_model
        module_context["provider"] = target_agent_record.provider if target_agent_record else target_provider
        module_context["agent_id"] = target_agent_id
        self.console.emit(
            "MODEL_REPLACEMENT",
            f"task_id={task.task_id} from={current_provider}/{current_model} to={target_provider}/{target_model} reason={recommendation.get('reason') or 'replacement_due'}",
        )
        return target_agent_id, target_agent_record, recommendation

    async def _provider_inventory_loop(self) -> None:
        while not self._provider_inventory_stop.is_set():
            try:
                self._refresh_provider_inventory_snapshot(force_refresh=True)
            except Exception as exc:
                self.log("warning", f"[INVENTORY] provider refresh failed: {exc}")
            await asyncio.sleep(self._provider_inventory_refresh_interval_sec)

    def _refresh_kpi_dashboard(self) -> dict[str, Any]:
        kpi_log = Path(getattr(self.kpi_events, "file_path", "memory_store/kpi_events.jsonl"))
        rolling = Path("core/mimo/profiles/rolling_kpi_store.json")
        summary = Path(getattr(self.orchestration_config, "kpi_dashboard_output_path", "memory_store/kpi_dashboard_24h.json") or "memory_store/kpi_dashboard_24h.json")
        dashboard = build_kpi_dashboard(kpi_log_path=kpi_log, rolling_kpi_path=rolling, summary_path=summary, delivery_snapshot=self.delivery_health_snapshot())
        self.kpi_events.write({"event_type": "kpi_dashboard_refresh", "tasks_total": dashboard.get("task_lifecycle", {}).get("tasks_total", 0), "rejection_rate": dashboard.get("trained_memory_rejection", {}).get("rejection_rate", 0.0), "dashboard_path": str(summary)})
        return dashboard

    async def _kpi_dashboard_loop(self) -> None:
        while not self._kpi_dashboard_stop.is_set():
            await asyncio.sleep(self._kpi_dashboard_interval_sec)
            try:
                self._refresh_kpi_dashboard()
            except Exception as exc:
                self.log("warning", f"[KPI] dashboard refresh failed: {exc}")

    def _kpi_rejection_summary(self) -> dict[str, Any]:
        counters = self.metrics.snapshot().get("counters", {})
        accepted = 0
        rejected = 0
        by_task: dict[str, dict[str, int]] = {}
        for key, value in counters.items():
            if not key.startswith("trained_memory."):
                continue
            parts = key.split(".")
            if len(parts) < 3:
                continue
            task_type = parts[1]
            bucket = by_task.setdefault(task_type, {"accepted": 0, "rejected": 0})
            if parts[2] == "accepted":
                accepted += int(value)
                bucket["accepted"] += int(value)
            elif parts[2] == "rejected":
                rejected += int(value)
                bucket["rejected"] += int(value)
        total = accepted + rejected
        rate = round(rejected / total, 4) if total else 0.0
        return {
            "summary_type": "trained_memory_rejection_summary",
            "accepted": accepted,
            "rejected": rejected,
            "rejection_rate": rate,
            "by_task": by_task,
        }

    def attach_local_agent(self, agent_id: str, agent: BaseAgent, agent_type: str = "custom", critical: bool = False, model_name: str = "local-small", provider: str = "local") -> None:
        self.local_agents[agent_id] = agent
        agent.set_host_bridge(self.host_bridge)
        setattr(agent, "orchestrator", self)
        if hasattr(agent, "set_api"):
            agent.set_api(self)
        if not self.registry.get(agent_id):
            self.registry.register(agent_id, agent_type, f"local://{agent_id}", agent.capabilities, critical=critical, model_name=model_name, provider=provider)
            self.metrics.register_agent(self.registry.get(agent_id))  # type: ignore[arg-type]
            
        # 2. Register as TPP Pod (Mesh Architecture)
        if hasattr(self.message_bus, "register_pod"):
            self.message_bus.register_pod(agent_id, agent.capabilities)
        self._ensure_agent_worker(agent_id)
        self.log("info", f"[KERNEL] Attached local agent pod: {agent_id} (TPP Enabled)")

    def qt_dev_box(self) -> QtDevBoxModule | None:
        module = self.module_manager.get_module("qt_dev_box")
        if isinstance(module, QtDevBoxModule):
            return module
        return None

    def _broadcast_pod_state(self, agent_id: str, status: AgentStatus, task_id: str | None = None) -> None:
        """Updates the TPP mesh with the current pod state."""
        if not hasattr(self.message_bus, "update_pod_state"):
            return
            
        # Calculate memory fingerprint (hash of recent thoughts/results)
        thoughts = self.session_memory.get(MemoryScope.AGENT, agent_id, "thoughts") or []
        fingerprint = hashlib.md5(str(thoughts).encode()).hexdigest()[:8]
        
        self.message_bus.update_pod_state(agent_id, status, task=task_id, fingerprint=fingerprint)

    def load_kernel_module(self, name: str) -> None:
        self.module_manager.load(name)

    def unload_kernel_module(self, name: str) -> None:
        self.module_manager.unload(name)

    def shutdown(self) -> None:
        self._agent_worker_stop.set()
        self._stop_postgres_watchdog()
        self._training_consolidation_stop.set()
        self._kpi_dashboard_stop.set()

    def loaded_kernel_modules(self) -> list[str]:
        return self.module_manager.loaded_modules()

    def _control_module(self) -> OrchestratorControlModule | None:
        module = self.module_manager.get_module("orchestrator_control")
        if isinstance(module, OrchestratorControlModule):
            return module
        return None

    def _memory_control_module(self) -> MemoryControlModule | None:
        module = self.module_manager.get_module("memory_control")
        if isinstance(module, MemoryControlModule):
            return module
        return None

    def _local_model_manager_module(self) -> LocalModelManagerModule | None:
        module = self.module_manager.get_module("local_model_manager")
        if isinstance(module, LocalModelManagerModule):
            return module
        return None

    async def submit_user_task_async(self, payload: object, source: str = "user_input") -> dict[str, object]:
        from .task_submission_api import create_standard_task, normalize_user_payload, validate_normalized_payload

        normalized = normalize_user_payload(payload)
        ok, issues = validate_normalized_payload(normalized)
        if not ok:
            message = "; ".join(issues) or "invalid_input"
            self.console.emit("INPUT_REJECTED", f"source={source} issues={message}")
            return {
                "status": "rejected",
                "message": "invalid or empty task payload",
                "issues": issues,
                "source": source,
            }

        session_id = str(normalized.get("session_id") or "default")
        idem_raw = json.dumps(normalized, sort_keys=True, ensure_ascii=True)
        idempotency_key = hashlib.sha256(idem_raw.encode("utf-8")).hexdigest()
        cache_key = f"submit:{idempotency_key}"
        cached = self.session_memory.get(MemoryScope.SESSION, session_id, cache_key)
        if isinstance(cached, dict) and cached.get("status") in {"done", "failed"}:
            self.console.emit("IDEMPOTENCY", f"cache hit for session={session_id} key={idempotency_key[:12]}")
            return cached

        message = normalized.get("message") or normalized.get("description")
        if isinstance(message, str) and message.strip():
            trigger_mod = self.module_manager.get_module("trigger_dispatcher")
            if isinstance(trigger_mod, TriggerDispatcherModule):
                triggered = trigger_mod.process_chat_input(message)
                if triggered:
                    normalized.update(triggered)

        task = create_standard_task(normalized)
        control = self._control_module()
        if control is not None:
            control.register_submission(task, source=source)
        memory_control = self._memory_control_module()
        if memory_control is not None:
            memory_control.register_submission(task, raw_payload=payload, normalized_payload=normalized, source=source)

        result = await self.run_async(task)
        self.session_memory.set(MemoryScope.SESSION, session_id, cache_key, result, ttl_sec=3600)
        return result

    def submit_user_task(self, payload: object, source: str = "user_input") -> dict[str, object]:
        from .task_submission_api import create_standard_task, normalize_user_payload, validate_normalized_payload

        normalized = normalize_user_payload(payload)
        ok, issues = validate_normalized_payload(normalized)
        if not ok:
            message = "; ".join(issues) or "invalid_input"
            self.console.emit("INPUT_REJECTED", f"source={source} issues={message}")
            return {
                "status": "rejected",
                "message": "invalid or empty task payload",
                "issues": issues,
                "source": source,
            }

        session_id = str(normalized.get("session_id") or "default")
        idem_raw = json.dumps(normalized, sort_keys=True, ensure_ascii=True)
        idempotency_key = hashlib.sha256(idem_raw.encode("utf-8")).hexdigest()
        cache_key = f"submit:{idempotency_key}"
        cached = self.session_memory.get(MemoryScope.SESSION, session_id, cache_key)
        if isinstance(cached, dict) and cached.get("status") in {"done", "failed"}:
            self.console.emit("IDEMPOTENCY", f"cache hit for session={session_id} key={idempotency_key[:12]}")
            return cached

        message = normalized.get("message") or normalized.get("description")
        if isinstance(message, str) and message.strip():
            trigger_mod = self.module_manager.get_module("trigger_dispatcher")
            if isinstance(trigger_mod, TriggerDispatcherModule):
                triggered = trigger_mod.process_chat_input(message)
                if triggered:
                    normalized.update(triggered)

        task = create_standard_task(normalized)
        control = self._control_module()
        if control is not None:
            control.register_submission(task, source=source)
        memory_control = self._memory_control_module()
        if memory_control is not None:
            memory_control.register_submission(task, raw_payload=payload, normalized_payload=normalized, source=source)
        result = self.run_sync(task)
        self.session_memory.set(MemoryScope.SESSION, session_id, cache_key, result, ttl_sec=3600)
        return result

    async def stream_user_task(self, payload: object, source: str = "user_input") -> AsyncIterator[dict[str, object]]:
        """Yield orchestrator console events while a submitted task is running."""
        loop = asyncio.get_running_loop()
        event_queue: asyncio.Queue[dict[str, object]] = asyncio.Queue()

        def console_listener(stage: str, message: str) -> None:
            event = {
                "type": "stream_event",
                "stage": stage,
                "message": message,
                "ts": datetime.now(UTC).isoformat(),
            }
            loop.call_soon_threadsafe(event_queue.put_nowait, event)

        self.console.listeners.append(console_listener)
        task_future = asyncio.create_task(self.submit_user_task_async(payload, source=source))
        heartbeat_interval_sec = 10.0
        last_heartbeat = loop.time()
        yield {"type": "stream_event", "stage": "ACCEPTED", "message": "task accepted by orchestrator"}

        try:
            while not task_future.done():
                try:
                    event = await asyncio.wait_for(event_queue.get(), timeout=0.25)
                    last_heartbeat = loop.time()
                    yield event
                except asyncio.TimeoutError:
                    now = loop.time()
                    if now - last_heartbeat >= heartbeat_interval_sec:
                        last_heartbeat = now
                        yield {
                            "type": "stream_event",
                            "stage": "HEARTBEAT",
                            "message": "task still running",
                            "ts": datetime.now(UTC).isoformat(),
                        }
                    continue

            while not event_queue.empty():
                yield event_queue.get_nowait()

            result = await task_future
            yield {"type": "final_result", "status": result.get("status", "unknown"), "result": result}
        except Exception as exc:
            yield {"type": "final_result", "status": "error", "message": str(exc)}
        finally:
            if console_listener in self.console.listeners:
                self.console.listeners.remove(console_listener)

    def monitoring_snapshot(self) -> dict[str, object]:
        control = self._control_module()
        if control is None:
            return {"source_of_truth": "orchestrator", "status": "control_module_not_loaded"}
        return control.finalize()

    @staticmethod
    def _normalize_provider(provider: str) -> str:
        p = provider.strip().lower()
        if p in {"antigravity", "antigravity-cli", "agy", "google", "gemini"}:
            return "antigravity"
        if p in {"mimo", "mimo-cli", "xiaomi", "github-copilot", "github-models"}:
            return "mimo"
        return p

    def _select_agent_by_provider_preference(self, capability: str, providers: list[str], exclude: set[str] | None = None, priority: Priority | str | None = None) -> str | None:
        skip = exclude or set()
        normalized = [self._normalize_provider(p) for p in providers]
        for provider in normalized:
            for record in self.registry.list_agents():
                if record.id in skip:
                    continue
                if capability not in record.capabilities:
                    continue
                if self._normalize_provider(record.provider) != provider:
                    continue
                if not is_agent_routable(record, priority):
                    continue
                if record.id in self.local_agents:
                    return record.id
        return None

    @staticmethod
    def _task_preferred_agent_id(task: Task) -> str | None:
        hints = task.routing_hints if isinstance(task.routing_hints, dict) else {}
        for key in ("preferred_agent_id", "batch_forced_agent_id", "forced_agent_id"):
            value = hints.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
        return None

    @staticmethod
    def _agent_hint_matches(record: Any, hint: str | None) -> bool:
        normalized = str(hint or "").strip().lower()
        if not normalized:
            return False
        haystack = " ".join([record.id, record.provider, record.model_name, *record.capabilities]).lower()
        tokens = [token for token in normalized.replace("-", " ").replace("_", " ").split() if token]
        if normalized in haystack:
            return True
        return any(token in haystack for token in tokens)

    def _parallel_candidates_for_task(self, task: Task) -> list[Any]:
        capability = task.required_capability or CAPABILITY_BY_TASK_TYPE[task.type]
        hints = task.routing_hints if isinstance(task.routing_hints, dict) else {}
        agent_hint = hints.get("agent_hint") if isinstance(hints, dict) else None
        candidates = []
        for record in self.registry.list_agents():
            if record.id not in self.local_agents:
                continue
            if capability not in record.capabilities:
                continue
            if not is_agent_routable(record, task.priority):
                continue
            candidates.append(record)
        if agent_hint:
            hinted = [record for record in candidates if self._agent_hint_matches(record, str(agent_hint))]
            if hinted:
                hinted_ids = {item.id for item in hinted}
                candidates = hinted + [record for record in candidates if record.id not in hinted_ids]
        return sorted(candidates, key=lambda record: self.scheduler.agent_score(record, capability), reverse=True)

    def _preassign_parallel_batch_agents(self, tasks: list[Task]) -> dict[str, str]:
        assignments: dict[str, str] = {}
        reserved: set[str] = set()
        reserved_models: set[tuple[str, str]] = set()
        candidate_map = {task.task_id: self._parallel_candidates_for_task(task) for task in tasks}

        def _priority_weight(item: Task) -> int:
            order = {Priority.CRITICAL: 4, Priority.HIGH: 3, Priority.NORMAL: 2, Priority.LOW: 1}
            return order.get(item.priority, 2)

        ordered = sorted(tasks, key=lambda item: (len(candidate_map.get(item.task_id, [])) or 999, -_priority_weight(item), item.task_id))
        for task in ordered:
            hints = task.routing_hints if isinstance(task.routing_hints, dict) else {}
            if not isinstance(hints, dict):
                task.routing_hints = {}
                hints = task.routing_hints
            existing = self._task_preferred_agent_id(task)
            if existing:
                assignments[task.task_id] = existing
                reserved.add(existing)
                existing_record = self.registry.get(existing)
                if existing_record is not None:
                    reserved_models.add((str(existing_record.provider), str(existing_record.model_name)))
                continue
            candidates = candidate_map.get(task.task_id, [])
            if not candidates:
                continue
            pool = [record for record in candidates if record.id not in reserved] or candidates
            diversified = [record for record in pool if (str(record.provider), str(record.model_name)) not in reserved_models]
            selected = (diversified or pool)[0]
            hints["preferred_agent_id"] = selected.id
            hints["batch_forced_agent_id"] = selected.id
            assignments[task.task_id] = selected.id
            reserved.add(selected.id)
            reserved_models.add((str(selected.provider), str(selected.model_name)))
        return assignments

    @staticmethod
    def _openai_template_catalog_path() -> Path:
        explicit = str(os.getenv("OPENAI_ORCHESTRATOR_TEMPLATES_PATH", "")).strip()
        if explicit:
            return Path(explicit)
        return Path("core/mimo/profiles/generated/openai_compatible/orchestrator_templates.json")

    def _load_openai_template_catalog(self) -> dict[str, Any]:
        path = self._openai_template_catalog_path()
        if not path.exists():
            return {}
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return {}
        return payload if isinstance(payload, dict) else {}

    @staticmethod
    def _openai_template_agent_id(model_name: str) -> str:
        safe = str(model_name or "").replace(".", "-").replace(":", "-").replace("/", "-").strip("-")
        safe = safe or "worker"
        return f"codex-openai-{safe}"[:64]

    @staticmethod
    def _openai_template_worker_limit() -> int:
        raw = str(os.getenv("AI_BRIDGE_OPENAI_CODE_WORKERS_MAX", "3")).strip()
        try:
            return max(0, int(raw))
        except ValueError:
            return 3

    def _detach_local_agent(self, agent_id: str) -> None:
        self.local_agents.pop(agent_id, None)
        self.registry.unregister(agent_id)
        self._agent_p2p_inbox.pop(agent_id, None)
        if hasattr(self.message_bus, "pods"):
            self.message_bus.pods.pop(agent_id, None)
        inboxes = getattr(self.message_bus, "_pod_inboxes", None)
        if isinstance(inboxes, dict):
            inboxes.pop(agent_id, None)

    def sync_openai_template_workers(self, *, enabled: bool = True, primary_model: str = "") -> dict[str, Any]:
        if not enabled:
            return {"enabled": False, "attached": [], "removed": [], "kept": sorted(self._openai_template_agent_ids)}

        payload = self._load_openai_template_catalog()
        role_map = payload.get("roles") if isinstance(payload.get("roles"), dict) else {}
        rows = role_map.get("code_parallel") if isinstance(role_map, dict) else []
        if not isinstance(rows, list):
            return {"enabled": True, "attached": [], "removed": [], "kept": sorted(self._openai_template_agent_ids)}

        primary = str(primary_model or "").strip()
        limit = self._openai_template_worker_limit()
        desired: list[tuple[str, str]] = []
        seen_models: set[str] = {primary} if primary else set()
        for row in rows:
            if not isinstance(row, dict):
                continue
            model_name = str(row.get("model_name") or "").strip()
            if not model_name or model_name in seen_models:
                continue
            seen_models.add(model_name)
            desired.append((self._openai_template_agent_id(model_name), model_name))
            if len(desired) >= limit:
                break

        desired_ids = {agent_id for agent_id, _ in desired}
        removed: list[str] = []
        for agent_id in sorted(self._openai_template_agent_ids - desired_ids):
            self._detach_local_agent(agent_id)
            removed.append(agent_id)

        attached: list[str] = []
        kept: list[str] = []
        for agent_id, model_name in desired:
            if agent_id in self.local_agents and self.registry.get(agent_id) is not None:
                kept.append(agent_id)
                continue
            self.attach_local_agent(
                agent_id,
                CodexAgent(agent_id),
                agent_type="codex",
                critical=False,
                model_name=model_name,
                provider="openai",
            )
            attached.append(agent_id)

        self._openai_template_agent_ids = desired_ids
        return {"enabled": True, "attached": attached, "removed": removed, "kept": kept}

    def _build_decomposition_advisory(self, task: Task) -> dict[str, object]:
        advisory_context: dict[str, object] = {}

        if self._testing_mode():
            advisory_context["sourcecraft"] = {"enabled": False, "should_delegate": False}
            advisory_context["sourcecraft_runtime"] = {"status": "testing_disabled"}
            advisory_context["local_llm"] = {
                "enabled": True,
                "ready": True,
                "should_delegate": False,
                "status": "ready",
                "recommended_owner": "local_llm",
                "recommended_model": "local-small",
                "preferred_model": "local-small",
                "task_family": str(getattr(getattr(task, "type", None), "value", "unknown")),
            }
            return advisory_context

        task_text = str(task.input.description or "").lower()
        sourcecraft_keywords = ("sourcecraft", "repo", "repository", "pull request", "issue", "release", "branch", "changelog", "quota", "status")
        is_sourcecraft_task = (
            str(getattr(task, "required_capability", "") or "").strip().lower() in {"sourcecraft", "repo_ops", "pr_flow", "release_flow", "issue_flow", "branch_governance"}
            or (task.type == TaskType.PLAN and any(keyword in task_text for keyword in sourcecraft_keywords))
        )
        fast_plan_task = task.type == TaskType.PLAN and not is_sourcecraft_task

        sourcecraft_module = self.module_manager.get_module("sourcecraft") if hasattr(self.module_manager, "get_module") else None
        if sourcecraft_module and hasattr(sourcecraft_module, "build_delegation_profile"):
            try:
                advisory_context["sourcecraft"] = sourcecraft_module.build_delegation_profile(
                    task,
                    {
                        "description": task.input.description,
                        "repo_path": task.context.repo_path,
                        "branch": task.context.branch,
                    },
                )
                if is_sourcecraft_task and hasattr(sourcecraft_module, "ensure_ready"):
                    advisory_context["sourcecraft_runtime"] = sourcecraft_module.ensure_ready(repo_path=task.context.repo_path or ".")
                else:
                    advisory_context["sourcecraft_runtime"] = {"status": "skipped_for_fast_plan"}
            except Exception:
                advisory_context["sourcecraft"] = {"enabled": False, "should_delegate": False}
                advisory_context["sourcecraft_runtime"] = {"status": "error"}

        local_llm_module = self.module_manager.get_module("local_llm") if hasattr(self.module_manager, "get_module") else None
        if local_llm_module and isinstance(local_llm_module, LocalLLMModule):
            try:
                advisory_payload = {
                    "description": task.input.description,
                    "repo_path": task.context.repo_path,
                    "branch": task.context.branch,
                }
                if fast_plan_task:
                    advisory_context["local_llm"] = local_llm_module._advisory_base(task, advisory_payload)
                    advisory_context["local_llm"]["decomposition"] = local_llm_module._heuristic_decomposition_draft(task, advisory_payload)
                    advisory_context["local_llm"]["decomposition_source"] = "heuristic_fast_plan"
                else:
                    advisory_context["local_llm"] = local_llm_module.build_decomposition_draft(task, advisory_payload)
                self.log("info", f"[LOCAL_LLM] First-layer decomposition draft generated for task {task.task_id}")
            except Exception as e:
                self.log("warning", f"[LOCAL_LLM] Failed to generate decomposition draft: {e}")
                advisory_context["local_llm"] = {"enabled": False, "ready": False, "should_delegate": False}

        template_catalog = self._load_openai_template_catalog()
        role_map = template_catalog.get("roles") if isinstance(template_catalog.get("roles"), dict) else {}
        if role_map:
            advisory_context["openai_compatible"] = {
                "enabled": True,
                "generated_at": template_catalog.get("generated_at"),
                "template_count": template_catalog.get("template_count", 0),
                "code_parallel_candidates": list(role_map.get("code_parallel", []))[:4],
                "review_candidates": list(role_map.get("review_primary", []))[:3],
                "plan_candidates": list(role_map.get("plan_primary", []))[:3],
                "test_candidates": list(role_map.get("test_primary", []))[:3],
                "docs_candidates": list(role_map.get("docs_primary", []))[:3],
                "research_candidates": list(role_map.get("research_primary", []))[:3],
            }

        return advisory_context

    def _materialize_mistral_delegation_plan(self, task: Task, advisory_context: dict[str, object]) -> ExecutionPlan | None:
        governance = advisory_context.get("mistral_governance")
        if not isinstance(governance, dict):
            return None
        if str(governance.get("selected_owner") or "").strip().lower() != "mistral_gateway":
            return None
        delegation_plan = governance.get("delegation_plan")
        if not isinstance(delegation_plan, list) or not delegation_plan:
            return None

        atomic_tasks: list[Task] = []
        previous_task_id: str | None = None
        for item in delegation_plan:
            if not isinstance(item, dict):
                continue
            raw_type = str(item.get("task_type") or task.type.value).strip().lower()
            try:
                task_type = TaskType(raw_type)
            except ValueError:
                task_type = task.type
            capability = CAPABILITY_BY_TASK_TYPE.get(task_type, raw_type)
            subtask = Task(
                task_type,
                TaskInput(str(item.get("objective") or task.input.description)),
                TaskContext(task.context.project, task.context.repo_path, task.context.branch),
                priority=task.priority,
                parent_task_id=task.task_id,
                required_capability=capability,
                dependencies=[previous_task_id] if previous_task_id else [],
                routing_hints={
                    "source": "mistral_gateway",
                    "delegate_to": str(item.get("delegate_to") or "local_llm"),
                    "mode": str(item.get("mode") or "subtask"),
                    "managed_by": "mistral_governance",
                },
                session_id=task.session_id,
            )
            atomic_tasks.append(subtask)
            previous_task_id = subtask.task_id

        if not atomic_tasks:
            return None

        return ExecutionPlan(
            root_task_id=task.task_id,
            atomic_tasks=atomic_tasks,
            draft_layers=[
                {
                    "name": "mistral_gateway_delegation",
                    "owner": "mistral",
                    "target": "local_llm",
                    "delegated_tasks": len(atomic_tasks),
                }
            ],
        )

    def create_execution_plan(self, task: Task) -> ExecutionPlan:
        self.console.emit("PLAN", "Задача проанализирована")
        advisory_context = self._build_decomposition_advisory(task)
        memory_control = self._memory_control_module()
        if memory_control is not None:
            memory_control.register_planning_draft(task, advisory_context, source="advisory_context")
        gateway_plan = self._materialize_mistral_delegation_plan(task, advisory_context)
        if gateway_plan is not None:
            self.console.emit("PLAN", f"Mistral gateway execution plan created: {len(gateway_plan.atomic_tasks)} tasks")
            if memory_control is not None:
                memory_control.register_decomposition(task, gateway_plan, source="mistral_gateway")
            return gateway_plan

        sourcecraft_module = self.module_manager.get_module("sourcecraft")
        if sourcecraft_module and hasattr(sourcecraft_module, "build_execution_plan"):
            try:
                sourcecraft_plan = sourcecraft_module.build_execution_plan(
                    task,
                    {
                        "description": task.input.description,
                        "repo_path": task.context.repo_path,
                        "branch": task.context.branch,
                    },
                )
                if sourcecraft_plan and getattr(sourcecraft_plan, "atomic_tasks", None):
                    tdd_policy = self.module_manager.get_module("tdd_policy")
                    if isinstance(tdd_policy, StrictTDDModule):
                        sourcecraft_plan = tdd_policy.enforce_plan(sourcecraft_plan)
                    readability_policy = self.module_manager.get_module("readability_policy")
                    if isinstance(readability_policy, CodeReadabilityModule):
                        sourcecraft_plan = readability_policy.enforce_plan(sourcecraft_plan)
                    self.console.emit("PLAN", f"SourceCraft execution plan created: {len(sourcecraft_plan.atomic_tasks)} tasks")
                    if memory_control is not None:
                        memory_control.register_decomposition(task, sourcecraft_plan, source="sourcecraft")
                    return sourcecraft_plan
            except Exception as e:
                self.console.emit("PLAN", f"SourceCraft planning fallback: {e}")

        # Try smart decomposition first (Higher level AI/Reasoning)
        smart_decomp = self.module_manager.get_module("smart_decomposer")
        if isinstance(smart_decomp, SmartDecomposerModule):
            try:
                plan = smart_decomp.decompose_task(task)
                if plan:
                    self.console.emit("PLAN", f"Умная декомпозиция (Reasoning): создано {len(plan.atomic_tasks)} задач")
                    if memory_control is not None:
                        memory_control.register_decomposition(task, plan, source="smart_decomposer")
                    return plan
            except Exception as e:
                self.console.emit("PLAN", f"Ошибка умной декомпозиции, используем fallback: {e}")

        # Fallback to TaskDecomposer which now better handles local_llm drafts
        plan = self.decomposer.decompose(task, advisory_context=advisory_context)
        
        # Apply Strict TDD Policy if loaded
        tdd_policy = self.module_manager.get_module("tdd_policy")
        if isinstance(tdd_policy, StrictTDDModule):
            plan = tdd_policy.enforce_plan(plan)
            
        # Apply Readability Policy
        readability_policy = self.module_manager.get_module("readability_policy")
        if isinstance(readability_policy, CodeReadabilityModule):
            plan = readability_policy.enforce_plan(plan)

        self.console.emit("PLAN", f"Создано атомарных задач: {len(plan.atomic_tasks)}")
        if memory_control is not None:
            memory_control.register_decomposition(task, plan, source="task_decomposer")

        return plan

    def _load_memory_context(self, task: Task, agent_id: str, *, provider: str = "", model_name: str = "") -> dict[str, object]:
        memory_control = self._memory_control_module()
        if memory_control is not None:
            return dict(memory_control.build_runtime_context(task, agent_id=agent_id, provider=provider, model_name=model_name))
        scope_name = (task.memory_scope or "task").lower()
        scope = MemoryScope.TASK
        if scope_name == "session":
            scope = MemoryScope.SESSION
        elif scope_name == "agent":
            scope = MemoryScope.AGENT
        elif scope_name == "capability":
            scope = MemoryScope.CAPABILITY

        if scope == MemoryScope.SESSION:
            identifier = task.session_id or "default"
        elif scope == MemoryScope.AGENT:
            identifier = agent_id
        elif scope == MemoryScope.CAPABILITY:
            identifier = task.required_capability or CAPABILITY_BY_TASK_TYPE[task.type]
        else:
            identifier = task.task_id

        context: dict[str, object] = {}
        if task.cache_policy != "write_only":
            for key in task.memory_keys:
                normalized = key.lower()
                if "thought" in normalized or normalized.endswith(":errors") or normalized == "errors":
                    continue
                value = self.session_memory.get(scope, identifier, key)
                if value is not None:
                    context[key] = value

        config = getattr(self, "orchestration_config", None)
        high_risk_trained_memory = bool(getattr(config, "high_risk_trained_memory_enabled", False)) if config else False
        task_type = task.type.value.lower()
        if high_risk_trained_memory or task_type in {"plan", "review", "test", "code", "docs", "research"}:
            trained_domain = self._training_memory_domain(task)
            token_limit = 180 if task.type in {TaskType.PLAN, TaskType.REVIEW, TaskType.TEST} else 120
            trained_brief = self.session_memory.hybrid.retrieve_trained_memory_brief(
                session_id=task.session_id or task.task_id,
                agent_id=agent_id,
                memory_domain=trained_domain,
                top_k=1,
                token_limit=token_limit,
                task_type=task_type,
                allow_trained_memory=high_risk_trained_memory,
                query_text=str(task.input.description or ''),
                files=[str(item).strip() for item in list(task.input.files or []) if str(item).strip()],
                constraints=[str(item).strip() for item in list(task.input.constraints or []) if str(item).strip()],
                acceptance_criteria=[str(item).strip() for item in list(task.input.acceptance_criteria or []) if str(item).strip()],
            )
            if trained_brief:
                context["trained_memory_domain"] = trained_domain
                context["trained_memory_brief"] = trained_brief
                context["trained_memory_trusted"] = len(trained_brief) >= 80 and "Quality:" in trained_brief
                context["trained_memory_disabled_for_risk"] = False
            else:
                context["trained_memory_disabled_for_risk"] = not high_risk_trained_memory
        else:
            context["trained_memory_disabled_for_risk"] = True

        capability = task.required_capability or CAPABILITY_BY_TASK_TYPE[task.type]
        reusable = self.session_memory.hybrid.retrieve_reusable_task_context(
            task=task,
            agent_id=f"shared:{capability}",
            capability=capability,
            top_k=2 if task.type in {TaskType.CODE, TaskType.REVIEW, TaskType.TEST} else 1,
            token_limit=160 if task.type in {TaskType.PLAN, TaskType.REVIEW, TaskType.TEST} else 140,
        )
        if reusable.get("matched") and str(reusable.get("brief") or "").strip():
            context["reusable_task_memory_brief"] = str(reusable.get("brief") or "")
            context["reusable_task_memory_similarity"] = float(reusable.get("similarity", 0.0) or 0.0)
            context["reusable_task_memory_fingerprint"] = str(reusable.get("fingerprint") or "")
            context["reusable_task_memory_count"] = int(reusable.get("count", 0) or 0)
        layered = self.layered_context_memory.build_context_pie(task, agent_id=agent_id, provider=provider, model_name=model_name)
        if layered.layered_context_brief:
            context["layered_context_brief"] = layered.layered_context_brief
        if layered.prompt_memory_brief:
            context["prompt_memory_brief"] = layered.prompt_memory_brief
        if layered.routing_memory_brief:
            context["routing_memory_brief"] = layered.routing_memory_brief
        if layered.execution_memory_brief:
            context["execution_memory_brief"] = layered.execution_memory_brief
        if layered.prompt_guidance:
            context["prompt_guidance"] = list(layered.prompt_guidance)
        return context

    def _model_usage_module(self) -> ModelUsageModule | None:
        module = self.module_manager.get_module("model_usage")
        if isinstance(module, ModelUsageModule):
            return module
        return None

    @staticmethod
    def _estimate_task_tokens(task: Task) -> int:
        return max(32, len(str(task.input)) // 4)

    def _enforce_model_budget_policy(
        self,
        task: Task,
        capability: str,
        choice: Any,
        agent_id: str,
        agent_record: Any,
        module_context: dict[str, object],
        fallback_count: int,
    ) -> tuple[str, Any, int, bool, AgentResult | None]:
        usage_module = self._model_usage_module()
        if usage_module is None:
            return agent_id, agent_record, fallback_count, False, None

        planned_tokens = self._estimate_task_tokens(task)
        checked_agents = {agent_id}
        fallback_used = False

        while True:
            model_name = agent_record.model_name if agent_record else choice.model_name
            policy = usage_module.evaluate_model_budget(model_name, planned_tokens=planned_tokens)
            module_context["model_budget"] = policy

            action = policy["action"]
            remaining_pct = policy["remaining_percentage"]
            if action == "ok":
                return agent_id, agent_record, fallback_count, fallback_used, None

            if action == "warn":
                self.console.emit("TOKEN_BUDGET", f"task_id={task.task_id} model={model_name} remaining={remaining_pct}% threshold=warn")
                return agent_id, agent_record, fallback_count, fallback_used, None

            if action == "reduce":
                self.console.emit("TOKEN_BUDGET", f"task_id={task.task_id} model={model_name} remaining={remaining_pct}% threshold=reduce")
                module_context["token_pressure"] = "reduce"
                return agent_id, agent_record, fallback_count, fallback_used, None

            self.console.emit("TOKEN_BUDGET", f"task_id={task.task_id} model={model_name} remaining={remaining_pct}% threshold=error")
            fallback_chain = self.provider_budget_router.preferred_providers(task, choice)
            fallback_agent_id = self._select_agent_by_provider_preference(capability, fallback_chain, exclude=checked_agents, priority=task.priority)
            if fallback_agent_id:
                fallback_record = self.registry.get(fallback_agent_id)
                if fallback_record is not None:
                    checked_agents.add(fallback_agent_id)
                    self.console.emit("FALLBACK", f"task_id={task.task_id} from={agent_id} to={fallback_agent_id} reason=token_budget_floor")
                    fallback_count += 1
                    fallback_used = True
                    agent_id = fallback_agent_id
                    agent_record = fallback_record
                    module_context["agent_id"] = agent_id
                    module_context["provider"] = agent_record.provider
                    module_context["model"] = agent_record.model_name
                    module_context["fallback"] = True
                    continue

            summary = f"Model {model_name} blocked: remaining token budget {remaining_pct}% is below floor {policy['error_below_percentage']}%"
            failed_result = AgentResult(
                task.task_id,
                agent_id,
                TaskStatus.FAILED,
                {
                    "summary": summary,
                    "files_changed": [],
                    "commands_run": [],
                    "test_results": [],
                    "diff": "",
                    "token_budget": policy,
                },
                0.0,
                [summary],
                [],
            )
            self.module_manager.after_task(task, failed_result, module_context)
            return agent_id, agent_record, fallback_count, fallback_used, failed_result

    def _find_fallback_agent(self, capability: str, providers: list[str], exclude: set[str], priority: Priority | str | None = None) -> str | None:
        for provider in providers:
            for record in self.registry.list_agents():
                if record.id in exclude:
                    continue
                if record.provider != provider:
                    continue
                if capability not in record.capabilities:
                    continue
                if not is_agent_routable(record, priority):
                    continue
                if record.id in self.local_agents:
                    return record.id
        return None

    async def _parallel_self_check(self, task: Task) -> dict[str, Any]:
        """Runs multiple safety and architectural checks in parallel."""
        import asyncio
        self.log("info", f"[SELF-CHECK] Orchestrating parallel pre-flight for task {task.task_id}")
        
        checks = {}
        
        # 1. Risk Advisor
        risk_mod = self.module_manager.get_module("risk_advisor")
        if isinstance(risk_mod, RiskAdvisorModule):
            checks["risk"] = asyncio.to_thread(risk_mod.evaluate_task, task)
            
        # 2. Intelligence (Complexity)
        intel_mod = self.module_manager.get_module("intelligence")
        if isinstance(intel_mod, AIIntelligenceModule):
            checks["complexity"] = asyncio.to_thread(intel_mod.estimate_complexity, task)
            
        # 3. Security Sentinel
        sec_mod = self.module_manager.get_module("security_sentinel")
        if isinstance(sec_mod, KernelSecuritySentinel):
            checks["security"] = asyncio.to_thread(sec_mod.validate_action, task)
            
        # 4. Dependency Manager (Static check)
        checks["system_deps"] = asyncio.to_thread(DependencyManager.find_missing)
        
        results = await asyncio.gather(*checks.values(), return_exceptions=True)
        final_report = dict(zip(checks.keys(), results))
        
        # Post-process results
        if isinstance(final_report.get("security"), bool) and not final_report["security"]:
            self.console.emit("SECURITY_ALERT", f"Task {task.task_id} blocked by security sentinel.")
            raise RuntimeError(f"Task {task.task_id} failed security validation.")
            
        return final_report

    def run_task(self, task: Task) -> AgentResult:
        # If we are in run_task (sync), we can't easily run the parallel self-check 
        # unless we wrap it in a loop. For truly multi-tasking orchestrator, 
        # we should prefer run_async.
        
        started_at = datetime.now(UTC)
        started_perf = time.perf_counter()
        lifecycle_logged = False
        lifecycle_payload: dict[str, Any] | None = None
        self.log("info", f"[PRE-FLIGHT] Verifying readiness for task {task.task_id}")
        session_id = task.session_id or task.task_id
        cache_guard_failure = self._cache_guard_failure(task)
        if cache_guard_failure is not None:
            return cache_guard_failure
        
        # Try to run parallel checks if we have a loop, otherwise skip or run sync
        try:
            loop = asyncio.get_event_loop()
            if loop.is_running():
                # We can't wait for it here if run_task is sync.
                pass 
        except RuntimeError:
            pass

        capability = task.required_capability or CAPABILITY_BY_TASK_TYPE[task.type]
        advisory_context = self._build_decomposition_advisory(task)
        mimo_model_name = task.assigned_model or "unknown"
        mimo_memory_context = advisory_context.get("local_llm") if isinstance(advisory_context, dict) else None
        selection_context = self.mimo_director.build_selection_context(
            mimo_model_name,
            task,
            current_budget=float(os.getenv("MIMO_REMAINING_BUDGET", "999999")),
            memory_context=mimo_memory_context,
        )
        local_llm_context = dict(advisory_context.get("local_llm") or {})
        local_ready = local_llm_context.get("ready")
        local_should_delegate = local_llm_context.get("should_delegate")
        local_task_family = local_llm_context.get("task_family")
        local_llm_context.update(selection_context)
        if local_ready is not None:
            local_llm_context["ready"] = local_ready
        if local_should_delegate is not None:
            local_llm_context["should_delegate"] = local_should_delegate
        if local_task_family is not None:
            local_llm_context["task_family"] = local_task_family
        advisory_context["local_llm"] = local_llm_context
        advisory_context["mimo"] = selection_context

        choice, mimo_recommendation = self._select_model_choice_with_mimo(
            task,
            advisory_context,
            float(os.getenv("MIMO_REMAINING_BUDGET", "999999")),
            mimo_memory_context,
        )
        if choice is None:
            blocked_by = getattr(mimo_recommendation, "blocked_by", None) or "policy"
            escalation = getattr(mimo_recommendation, "escalation_reason", None) or getattr(mimo_recommendation, "reason", "mimo_blocked")
            message = f"MIMO blocked model selection: blocked_by={blocked_by} escalation={escalation}"
            self.console.emit("MODEL_SELECTION", message)
            return AgentResult(task.task_id, "orchestrator", TaskStatus.FAILED, {"summary": message, "files_changed": [], "commands_run": [], "test_results": [], "diff": ""}, 0.0, [message], [])
        choice = self.mimo_director.validate_and_correct(
            choice,
            task,
            current_budget=float(os.getenv("MIMO_REMAINING_BUDGET", "999999")),
            memory_context=mimo_memory_context,
        )

        self.console.emit(
            "MODEL_SELECTION",
            f"task_id={task.task_id} task_type={task.type.value} detected_keywords={choice.detected_keywords or []} "
            f"matched_high_risk_rules={choice.matched_high_risk_rules or []} "
            f"matched_low_risk_exemptions={choice.matched_low_risk_exemptions or []} "
            f"final_complexity={choice.complexity.value} selected_provider={choice.provider} selected_model={choice.model_name} "
            f"secondary_review={choice.requires_secondary_review} reason={choice.reason}",
        )

        runtime_usage = self._runtime_usage_hints(task)
        module_context: dict[str, object] = {
            "selected_provider": choice.provider,
            "selected_model": choice.model_name,
            "reason": choice.reason,
            "prompt_version": str(runtime_usage.get("prompt_version") or "v1"),
            "context_version": str(runtime_usage.get("context_version") or self._default_context_version(task)),
            **runtime_usage,
            **advisory_context,
        }
        state_snapshot = self.state_store.save_session_state(
            session_id,
            {"task_id": task.task_id, "status": "running", "task_type": task.type.value, "agent_id": None},
            prompt_version=str(module_context.get("prompt_version") or "v1"),
            context_version=str(module_context.get("context_version") or self._default_context_version(task)),
        )
        self.module_manager.before_task(task, module_context)

        self.autoscaler.ensure_capacity(capability)
        decision = self.scheduler.schedule(task)
        if decision.requires_orchestrator:
            self.console.emit("SCHEDULER", f"Orchestrator route: {decision.reason}")
        else:
            self.console.emit("SCHEDULER", f"P2P route allowed: {decision.reason}")

        forced_agent_id = self._task_preferred_agent_id(task)
        if forced_agent_id:
            forced_record = self.registry.get(forced_agent_id)
            if forced_record and capability in forced_record.capabilities and is_agent_routable(forced_record, task.priority) and forced_agent_id in self.local_agents:
                acceptance = TaskAcceptance(task.task_id, TaskStatus.ACCEPTED, forced_agent_id, self.router.estimate_complexity(task), "Task accepted (parallel batch routing)")
            else:
                acceptance = self.router.route(task)
        else:
            preferred_providers = self.provider_budget_router.preferred_providers(task, choice)
            preferred_agent_id = self._select_agent_by_provider_preference(capability, preferred_providers, priority=task.priority)
            if preferred_agent_id:
                acceptance = TaskAcceptance(task.task_id, TaskStatus.ACCEPTED, preferred_agent_id, self.router.estimate_complexity(task), "Task accepted (provider budget routing)")
            else:
                acceptance = self.router.route(task)
        if acceptance.status == TaskStatus.REJECTED or not acceptance.assigned_agent:
            self.console.emit("ROUTING", acceptance.message)
            self.live_trace_rows.append(
                {
                    "task_id": task.task_id,
                    "task_type": task.type.value,
                    "detected_keywords": choice.detected_keywords or [],
                    "matched_high_risk_rules": choice.matched_high_risk_rules or [],
                    "matched_low_risk_exemptions": choice.matched_low_risk_exemptions or [],
                    "final_complexity": choice.complexity.value,
                    "selected_provider": choice.provider,
                    "selected_model": choice.model_name,
                    "router_agent": None,
                    "router_provider": None,
                    "fallback": False,
                    "secondary_review": choice.requires_secondary_review,
                    "reason": acceptance.message,
                }
            )
            failed_result = AgentResult(task.task_id, "orchestrator", TaskStatus.FAILED, {"summary": acceptance.message, "files_changed": [], "commands_run": [], "test_results": [], "diff": ""}, 0.0, [acceptance.message], [])
            self.module_manager.after_task(task, failed_result, module_context)
            return failed_result

        agent_id = acceptance.assigned_agent
        agent_record = self.registry.get(agent_id)
        selected_provider_norm = self._normalize_provider(choice.provider)
        routed_provider_norm = self._normalize_provider(agent_record.provider if agent_record else choice.provider)
        fallback = bool(selected_provider_norm != routed_provider_norm)
        fallback_count = 1 if fallback else 0

        module_context["agent_id"] = agent_id
        module_context["provider"] = agent_record.provider if agent_record else choice.provider
        module_context["model"] = agent_record.model_name if agent_record else choice.model_name
        module_context["fallback"] = fallback

        agent_id, agent_record, replacement = self._apply_model_replacement_policy(
            task,
            capability,
            choice,
            agent_id,
            agent_record,
            module_context,
            allow_same_agent=True,
        )
        if replacement is not None:
            fallback = True
            fallback_count += 1

        self.console.emit(
            "ROUTING",
            f"task_id={task.task_id} router_agent={agent_id} router_provider={agent_record.provider if agent_record else '-'} "
            f"fallback={fallback} secondary_review={choice.requires_secondary_review}",
        )

        # Pre-flight provider diagnostics: verify DNS/TCP/API/model readiness before spending a task attempt.
        provider = self._normalize_provider(agent_record.provider if agent_record else choice.provider)
        preflight_live = os.getenv("AI_BRIDGE_PREFLIGHT_LIVE_PROBE", "true").strip().lower() in {"1", "true", "yes", "on"}
        if self._testing_mode():
            preflight_live = False
        provider_health = self.availability.check_provider(provider, live=preflight_live)
        module_context["availability_preflight"] = provider_health.as_dict()
        provider_ready = provider_health.status in {ProviderStatus.HEALTHY, ProviderStatus.DEGRADED}
        if not provider_ready:
            diag = provider_health.as_dict()
            self.console.emit(
                "EXECUTION",
                f"Provider {provider} is not ready ({provider_health.status.value}: {provider_health.error or 'no details'}). Trying fallback providers.",
            )
            fallback_chain = self.provider_budget_router.preferred_providers(task, choice)
            selected_fallback_id = None
            selected_fallback_record = None
            selected_fallback_health = None

            for candidate_provider in fallback_chain:
                fallback_agent_id = self._select_agent_by_provider_preference(capability, [candidate_provider], exclude={agent_id}, priority=task.priority)
                if not fallback_agent_id:
                    continue
                fallback_record = self.registry.get(fallback_agent_id)
                fallback_provider = self._normalize_provider(fallback_record.provider if fallback_record else "")
                if not fallback_provider:
                    continue
                fallback_health = self.availability.check_provider(fallback_provider, live=preflight_live)
                fallback_ready = fallback_health.status in {ProviderStatus.HEALTHY, ProviderStatus.DEGRADED}
                if not fallback_ready:
                    self.console.emit(
                        "EXECUTION",
                        f"Fallback provider {fallback_provider} is not ready ({fallback_health.status.value}: {fallback_health.error or 'no details'}). Skipping.",
                    )
                    continue
                selected_fallback_id = fallback_agent_id
                selected_fallback_record = fallback_record
                selected_fallback_health = fallback_health
                break

            if selected_fallback_id and selected_fallback_record:
                self.console.emit("FALLBACK", f"task_id={task.task_id} from={agent_id} to={selected_fallback_id} reason=preflight_{provider_health.status.value}")
                fallback_count += 1
                agent_id = selected_fallback_id
                agent_record = selected_fallback_record
                module_context["agent_id"] = agent_id
                module_context["provider"] = agent_record.provider
                module_context["model"] = agent_record.model_name
                if selected_fallback_health is not None:
                    module_context["fallback_availability_preflight"] = selected_fallback_health.as_dict()
            else:
                summary = f"Provider {provider} unavailable and no ready fallback"
                failed_result = AgentResult(task.task_id, agent_id, TaskStatus.FAILED, {"summary": summary, "files_changed": [], "commands_run": [], "test_results": [], "diff": "", "provider_diagnostics": diag}, 0.0, [f"Provider {provider} unavailable: {provider_health.status.value}: {provider_health.error or 'no details'}"], [])
                self.module_manager.after_task(task, failed_result, module_context)
                return failed_result

        agent_id, agent_record, fallback_count, budget_fallback, budget_failed_result = self._enforce_model_budget_policy(
            task,
            capability,
            choice,
            agent_id,
            agent_record,
            module_context,
            fallback_count,
        )
        if budget_failed_result is not None:
            return budget_failed_result
        fallback = fallback or budget_fallback or fallback_count > 0
        memory_control = self._memory_control_module()
        if memory_control is not None:
            memory_control.register_routing_outcome(
                task,
                selected_provider=str(choice.provider),
                selected_model=str(choice.model_name),
                routed_agent=agent_id,
                routed_provider=str(agent_record.provider if agent_record else choice.provider),
                routed_model=str(agent_record.model_name if agent_record else choice.model_name),
                reason=str(choice.reason),
                fallback_count=fallback_count,
            )

        self.live_trace_rows.append(
            {
                "task_id": task.task_id,
                "task_type": task.type.value,
                "detected_keywords": choice.detected_keywords or [],
                "matched_high_risk_rules": choice.matched_high_risk_rules or [],
                "matched_low_risk_exemptions": choice.matched_low_risk_exemptions or [],
                "final_complexity": choice.complexity.value,
                "selected_provider": choice.provider,
                "selected_model": choice.model_name,
                "router_agent": agent_id,
                "router_provider": agent_record.provider if agent_record else None,
                "fallback": fallback,
                "secondary_review": choice.requires_secondary_review,
                "reason": choice.reason,
            }
        )

        if agent_record:
            agent_record.metrics.queue_depth = max(0, agent_record.metrics.queue_depth - 1)
            if task.assigned_model:
                agent_record.metrics.model_name = task.assigned_model
            self.lifecycle.mark_busy(agent_record, task)
            self.console.emit("EXECUTION", f"task_id={task.task_id} agent={agent_id} stage=start")
            self.console.agent_status(agent_record, task, progress=35, stage="выполняет задачу")

        try:
            agent = self.local_agents.get(agent_id)
            if not agent:
                failed_result = AgentResult(task.task_id, agent_id, TaskStatus.FAILED, {"summary": "No local executor for routed agent", "files_changed": [], "commands_run": [], "test_results": [], "diff": ""}, 0.0, ["No local executor"], [])
                self.module_manager.after_task(task, failed_result, module_context)
                return failed_result
            memory_context = self._load_memory_context(
                task,
                agent_id,
                provider=str(agent_record.provider if agent_record else choice.provider),
                model_name=str(agent_record.model_name if agent_record else choice.model_name),
            )

            selected_provider = self._normalize_provider(agent_record.provider if agent_record else choice.provider)
            if selected_provider == "ai_kernel":
                target_model = str(agent_record.model_name if agent_record else choice.model_name)
                try:
                    if not self.ai_kernel_bridge.ensure_ready(target_model):
                        failed_result = AgentResult(task.task_id, agent_id, TaskStatus.FAILED, {"summary": "AI Kernel backend is not ready", "files_changed": [], "commands_run": [], "test_results": [], "diff": ""}, 0.0, ["ai_kernel_not_ready"], [])
                        self.module_manager.after_task(task, failed_result, module_context)
                        return failed_result
                except Exception as exc:
                    self.log("warning", f"[AI_KERNEL] preflight failed: {exc}")

            # TPP: Mark pod as BUSY
            self._broadcast_pod_state(agent_id, AgentStatus.BUSY, task_id=task.task_id)
            
            result = self._run_local_agent_via_delivery(task, agent_id, capability, agent, memory_context)
            
            # TPP: Mark pod as READY
            self._broadcast_pod_state(agent_id, AgentStatus.READY)

            is_google_cli = bool(agent_record and agent_record.provider in {"antigravity", "antigravity-cli", "agy"})
            result_errors = " ".join(result.errors or [])
            classified = ""
            if result_errors:
                try:
                    from .external_ai_bridge import ExternalAIBridge
                    classified = ExternalAIBridge.classify_error(result_errors, task=task, api=self, model=result.model_name or "unknown")
                except Exception:
                    classified = ""

            if result.status == TaskStatus.FAILED:
                source_provider = self._normalize_provider(agent_record.provider if agent_record else choice.provider)
                failed_model_name = str(result.model_name or getattr(task, 'assigned_model', '') or module_context.get("model") or choice.model_name or "")
                if classified:
                    self.provider_budget_router.mark_failure(task, source_provider, classified, detail="; ".join(result_errors), model_name=failed_model_name)
                    self.availability.record_failure(source_provider, classified, result_errors)
                    self.model_replacement_policy.register_failure(source_provider, failed_model_name, classified)
                    manager = self._local_model_manager_module()
                    if manager is not None:
                        try:
                            manager.handle_failure(source_provider, failed_model_name, result_errors, task_id=task.task_id)
                        except Exception as exc:
                            self.log("warning", f"[LOCAL_MODEL_MANAGER] failure hook failed: {exc}")
                    if source_provider == "ai_kernel":
                        try:
                            recovered = self.ai_kernel_bridge.ensure_ready(failed_model_name)
                            if recovered:
                                self.log("info", f"[AI_KERNEL] Failure recovery restored readiness for {failed_model_name}.")
                            else:
                                self.log("warning", f"[AI_KERNEL] Failure recovery did not restore readiness for {failed_model_name}.")
                        except Exception as exc:
                            self.log("warning", f"[AI_KERNEL] failure recovery hook failed: {exc}")
                    self._update_model_replacement_snapshot()

                # Proactive Soft Fallback for all critical/high failures or quota issues
                should_fallback = (
                    classified in {"quota_exhaustion", "auth_fail", "api_timeout", "tcp_timeout"}
                    or (is_google_cli and classified in TIMEOUT_ERROR_TYPES)
                    or (task.priority in {Priority.HIGH, Priority.CRITICAL} and result.status == TaskStatus.FAILED)
                )

                if should_fallback:
                    retry_agent_id, retry_agent_record, replacement = self._apply_model_replacement_policy(
                        task,
                        capability,
                        choice,
                        agent_id,
                        agent_record,
                        module_context,
                        failure_reason=classified or 'probe_failed',
                        exclude_agents=set(),
                        allow_same_agent=True,
                    )
                    if replacement is None:
                        fallback_chain = self.provider_budget_router.preferred_providers(task, choice)
                        fallback_agent_id = self._select_agent_by_provider_preference(capability, fallback_chain, exclude={agent_id}, priority=task.priority)
                        retry_agent_id = fallback_agent_id or agent_id
                        retry_agent_record = self.registry.get(retry_agent_id) if fallback_agent_id else agent_record
                    if retry_agent_id:
                        self.console.emit("FALLBACK", f"task_id={task.task_id} from={agent_id} to={retry_agent_id} reason={classified or 'failure'}")
                        fallback_count += 1
                        fallback_agent = self.local_agents.get(retry_agent_id)
                        if fallback_agent:
                            agent_id = retry_agent_id
                            agent_record = retry_agent_record or agent_record
                            fallback_provider = str(agent_record.provider if agent_record else choice.provider)
                            fallback_model = str(agent_record.model_name if agent_record else choice.model_name)
                            module_context["agent_id"] = agent_id
                            module_context["provider"] = fallback_provider
                            module_context["model"] = fallback_model
                            memory_context = self._load_memory_context(
                                task,
                                agent_id,
                                provider=fallback_provider,
                                model_name=fallback_model,
                            )
                            result = self._run_local_agent_via_delivery(task, agent_id, capability, fallback_agent, memory_context)
                            result_errors = " ".join(result.errors or [])
                            if result_errors:
                                try:
                                    from .external_ai_bridge import ExternalAIBridge
                                    classified = ExternalAIBridge.classify_error(result_errors)
                                except Exception:
                                    pass
            else:
                success_provider = self._normalize_provider(agent_record.provider if agent_record else choice.provider)
                self.provider_budget_router.register_success(task, success_provider)
                self.model_replacement_policy.register_success(success_provider, str(result.model_name or getattr(task, 'assigned_model', '') or module_context.get("model") or choice.model_name or ""))
                self._update_model_replacement_snapshot()
            quality = self.quality.analyze(task, result)
            if agent_record:
                agent_record.metrics.quality_score = quality.score
                self.metrics.record_result(agent_record, result)
                self.kpi.apply_priority_policy(agent_record)
            self.results[task.task_id] = result
            try:
                self.mimo_director.register_execution_result(
                    result.model_name or choice.model_name,
                    result.status == TaskStatus.DONE,
                    time.perf_counter() - started_perf,
                    task=task,
                    quality_score=quality.score,
                    provider=agent_record.provider if agent_record else choice.provider,
                )
            except Exception:
                pass
            command_summary = result.output.get("summary", "")
            raw_thoughts = result.output.get("thoughts")
            if raw_thoughts:
                if isinstance(raw_thoughts, list):
                    for item in raw_thoughts:
                        self.session_memory.hybrid.append_agent_thought(session_id=task.session_id or task.task_id, agent_id=agent_id, thought=str(item))
                else:
                    self.session_memory.hybrid.append_agent_thought(session_id=task.session_id or task.task_id, agent_id=agent_id, thought=str(raw_thoughts))
            if result.errors:
                for error in result.errors:
                    self.session_memory.hybrid.append_agent_error(session_id=task.session_id or task.task_id, agent_id=agent_id, error=str(error))
            self.session_memory.hybrid.remember_command(
                session_id=task.session_id or task.task_id,
                agent_id=agent_id,
                command=f"task:{task.type.value}",
                result={"summary": command_summary, "status": result.status.value},
                success=result.status == TaskStatus.DONE,
            )
            if result.status == TaskStatus.DONE and command_summary.strip():
                try:
                    self.session_memory.hybrid.store_reusable_task_memory(
                        task=task,
                        agent_id=agent_id,
                        summary=command_summary,
                        quality_score=quality.score,
                        provider=result.provider or (agent_record.provider if agent_record else choice.provider),
                        model_name=result.model_name or (agent_record.model_name if agent_record else choice.model_name),
                    )
                except Exception as exc:
                    self.log("warning", f"[MEMORY] Failed to store reusable task memory: {exc}")
            if memory_control is not None:
                memory_control.register_result(
                    task,
                    result,
                    quality_score=quality.score,
                    fallback_count=fallback_count,
                    latency_ms=(time.perf_counter() - started_perf) * 1000.0,
                )
            if task.cache_policy in {"write_only", "read_write"}:
                scope_name = (task.memory_scope or "task").lower()
                scope = MemoryScope.TASK
                if scope_name == "session":
                    scope = MemoryScope.SESSION
                elif scope_name == "agent":
                    scope = MemoryScope.AGENT
                elif scope_name == "capability":
                    scope = MemoryScope.CAPABILITY

                if scope == MemoryScope.SESSION:
                    identifier = task.session_id or "default"
                elif scope == MemoryScope.AGENT:
                    identifier = agent_id
                elif scope == MemoryScope.CAPABILITY:
                    identifier = task.required_capability or CAPABILITY_BY_TASK_TYPE[task.type]
                else:
                    identifier = task.task_id

                self.session_memory.set(scope, identifier, "last_result", result.as_dict(), ttl_sec=task.memory_ttl_sec)
                self.session_memory.set(scope, identifier, "last_summary", result.output.get("summary", ""), ttl_sec=task.memory_ttl_sec)
            self.console.emit("EXECUTION", f"task_id={task.task_id} agent={agent_id} status={result.status.value}")

            resolved_record = self.registry.get(result.agent_id)
            if resolved_record:
                result.provider = resolved_record.provider
                result.model_name = resolved_record.model_name
                module_context["agent_id"] = result.agent_id
                module_context["provider"] = resolved_record.provider
                module_context["model"] = resolved_record.model_name
            self.module_manager.after_task(task, result, module_context)
            finished_at = datetime.now(UTC)
            latency_ms = round((time.perf_counter() - started_perf) * 1000.0, 2)
            model_usage_state = self.module_state().get("model_usage", {})
            history = model_usage_state.get("history", []) if isinstance(model_usage_state, dict) else []
            tokens_used = None
            estimated_cost_usd = None
            cost_components = {}
            cached_input_tokens = 0
            uncached_input_tokens = 0
            cache_hit_rate = 0.0
            cache_miss_reason = ""
            prompt_version = str(module_context.get("prompt_version") or "v1")
            context_version = str(module_context.get("context_version") or self._default_context_version(task))
            if isinstance(history, list) and history:
                for item in reversed(history):
                    if isinstance(item, dict) and item.get("task_id") == task.task_id:
                        tokens_used = item.get("tokens_used")
                        estimated_cost_usd = item.get("estimated_cost_usd")
                        cost_components = dict(item.get("cost_components") or {})
                        cached_input_tokens = int(item.get("cached_input_tokens") or 0)
                        uncached_input_tokens = int(item.get("uncached_input_tokens") or 0)
                        cache_hit_rate = float(item.get("cache_hit_rate") or 0.0)
                        cache_miss_reason = str(item.get("cache_miss_reason") or "")
                        prompt_version = str(item.get("prompt_version") or prompt_version)
                        context_version = str(item.get("context_version") or context_version)
                        break
            lifecycle_payload = {
                "event_type": "task_lifecycle",
                "task_id": task.task_id,
                "task_type": task.type.value,
                "priority": task.priority.value,
                "status": result.status.value,
                "agent_id": result.agent_id,
                "provider": result.provider or module_context.get("provider"),
                "model": result.model_name or module_context.get("model"),
                "fallback_count": fallback_count,
                "fallback_used": fallback_count > 0,
                "started_at": started_at.isoformat(),
                "finished_at": finished_at.isoformat(),
                "latency_ms": latency_ms,
                "tokens_used": tokens_used,
                "estimated_cost_usd": estimated_cost_usd,
                "cost_components": cost_components,
                "cached_input_tokens": cached_input_tokens,
                "uncached_input_tokens": uncached_input_tokens,
                "cache_hit_rate": cache_hit_rate,
                "cache_miss_reason": cache_miss_reason,
                "prompt_version": prompt_version,
                "context_version": context_version,
                "errors_count": len(result.errors or []),
            }
            self.kpi_events.write(lifecycle_payload)
            self.kpi_events.append_fallback(lifecycle_payload)
            guard_decision = self.cache_guard.observe(
                session_id=session_id,
                uncached_input_tokens=uncached_input_tokens,
                cached_input_tokens=cached_input_tokens,
                cache_hit_rate=cache_hit_rate,
            )
            if cache_miss_reason:
                self.state_store.record_invalidation(session_id, reason=cache_miss_reason, payload={"task_id": task.task_id, "cache_hit_rate": cache_hit_rate, "uncached_input_tokens": uncached_input_tokens, "cached_input_tokens": cached_input_tokens})
            if guard_decision.get("action") == GuardAction.HARD_STOP.value:
                self.state_store.record_invalidation(session_id, reason="CACHE_GUARD_HARD_STOP", payload={"task_id": task.task_id, "consecutive_misses": guard_decision.get("consecutive_misses")})
            self.state_store.save_session_state(
                session_id,
                {"task_id": task.task_id, "status": result.status.value, "task_type": task.type.value, "agent_id": result.agent_id, "guard_action": guard_decision.get("action")},
                prompt_version=prompt_version,
                context_version=context_version,
                expected_version=int(state_snapshot.get("version") or 1),
            )
            lifecycle_logged = True

            rejection_summary = self._kpi_rejection_summary()
            self.kpi_events.write(rejection_summary)
            self.kpi_events.write_summary({**rejection_summary, "summary_path": str(self.kpi_events.summary_path) if getattr(self.kpi_events, "summary_path", None) else ""})

            if choice.requires_secondary_review:
                self.console.emit(
                    "SECONDARY_REVIEW",
                    f"task_id={task.task_id} enabled=true reason={choice.reason}",
                )

            if result.status == TaskStatus.DONE and quality.passed:
                self._enqueue_training_consolidation(task, result)
            self.memory_consolidator.consolidate(session_id=task.session_id or task.task_id, agent_id=agent_id)
            if hasattr(self.message_bus, "publish_session_insights"):
                self.message_bus.publish_session_insights(task.session_id or task.task_id, {"task_id": task.task_id, "agent_id": agent_id, "summary": command_summary, "status": result.status.value})
            self.session_memory.hybrid.clear_session_thoughts(session_id=task.session_id or task.task_id)

            if not quality.passed:
                self.console.emit("REVIEW", f"Качество ниже порога: {', '.join(quality.issues)}")
            ok, fix_task = self.feedback.evaluate(task, result)
            if not ok and fix_task:
                self.console.emit("FIX", "Найдены ошибки, создана задача исправления")
                fix_result = self.run_task(fix_task)
                if fix_result.status == TaskStatus.DONE:
                    return AgentResult(task.task_id, fix_result.agent_id, TaskStatus.DONE, fix_result.output, min(0.8, fix_result.confidence), fix_result.errors, fix_result.next_recommendations, fix_result.provider, fix_result.model_name)
            return result
        finally:
            if not lifecycle_logged:
                fallback_payload = lifecycle_payload or {
                    "event_type": "task_lifecycle",
                    "task_id": task.task_id,
                    "task_type": task.type.value,
                    "priority": task.priority.value,
                    "status": "unknown",
                    "agent_id": getattr(locals().get("result", None), "agent_id", agent_id if 'agent_id' in locals() else "orchestrator"),
                    "provider": module_context.get("provider") if 'module_context' in locals() else None,
                    "model": module_context.get("model") if 'module_context' in locals() else None,
                    "fallback_count": fallback_count if 'fallback_count' in locals() else 0,
                    "fallback_used": bool(fallback_count) if 'fallback_count' in locals() else False,
                    "started_at": started_at.isoformat(),
                    "finished_at": datetime.now(UTC).isoformat(),
                    "latency_ms": round((time.perf_counter() - started_perf) * 1000.0, 2),
                    "tokens_used": None,
                    "errors_count": len(getattr(locals().get("result", None), "errors", []) or []),
                    "task_lifecycle_fallback": True,
                }
                try:
                    self.kpi_events.write(fallback_payload)
                    self.kpi_events.append_fallback(fallback_payload)
                except Exception:
                    pass
            if agent_record:
                self.lifecycle.mark_idle(agent_record)
                self.autoscaler.scale_down_idle()
            self.log("info", f"[POST-FLIGHT] Task {task.task_id} lifecycle complete")

    async def run_task_async(self, task: Task) -> AgentResult:
        """Run the complete synchronous task lifecycle without blocking the event loop."""
        return await asyncio.to_thread(self.run_task, task)

    async def run_plan_parallel(self, plan: ExecutionPlan) -> dict[str, Any]:
        """Executes independent branches of a DAG plan in parallel using run_task_async."""
        import asyncio
        self.live_trace_rows = []
        self.console.emit("AGENTS", f"Найдено агентов: {len(self.registry.list_agents())}, доступно: {len(self.registry.ready_agents())}")
        self.healthcheck.check_all()

        completed: set[str] = set()
        pending = {task.task_id: task for task in plan.atomic_tasks}
        final_results: list[AgentResult] = []
        results_by_task_id: dict[str, AgentResult] = {}
        batch_no = 0
        total_tasks = len(plan.atomic_tasks)

        while pending:
            ready_tasks = [task for task in pending.values() if all(dep in completed for dep in task.dependencies)]
            if not ready_tasks:
                raise RuntimeError("Task graph has unresolved dependencies or cycles")

            usage_module = self._model_usage_module()
            if usage_module is not None and usage_module.should_reduce_parallelism() and len(ready_tasks) > 1:
                self.console.emit("THROTTLE", f"Token budget is low; reducing parallel batch from {len(ready_tasks)} to 1")
                ready_tasks = ready_tasks[:1]

            assignments = self._preassign_parallel_batch_agents(ready_tasks)
            memory_control = self._memory_control_module()
            if memory_control is not None and assignments:
                try:
                    memory_control.prepare_parallel_batch(ready_tasks, assignments, registry=self.registry)
                except Exception:
                    pass
            batch_no += 1
            ready_ids = ", ".join(task.task_id[:8] for task in ready_tasks)
            if assignments:
                routed = ", ".join(f"{task.task_id[:8]}->{agent_id}" for task, agent_id in ((task, assignments.get(task.task_id, "")) for task in ready_tasks) if agent_id)
                if routed:
                    self.console.emit("PARALLEL_ROUTE", f"Batch {batch_no}: preassigned agents | {routed}")
            self.console.emit(
                "PARALLEL",
                f"Batch {batch_no}: starting {len(ready_tasks)} task(s) in parallel | ready={ready_ids} | completed={len(completed)}/{total_tasks}",
            )
            self.console.progress(
                "Parallel batches",
                len(completed),
                total_tasks,
                details=f"batch {batch_no} queued {len(ready_tasks)} task(s)",
            )

            handoff_count = self._dispatch_dependency_handoffs(ready_tasks, results_by_task_id)
            if handoff_count:
                self.console.emit("P2P_HANDOFF", f"Batch {batch_no}: dispatched {handoff_count} dependency handoff message(s)")
            results = await asyncio.gather(*(self.run_task_async(t) for t in ready_tasks))

            final_results.extend(results)
            for item in results:
                results_by_task_id[item.task_id] = item

            succeeded = sum(1 for r in results if r.status == TaskStatus.DONE)
            failed = len(results) - succeeded
            self.console.emit(
                "PARALLEL",
                f"Batch {batch_no}: finished | ok={succeeded} | failed={failed} | total_done={len(final_results)}",
            )
            self.console.progress(
                "Parallel batches",
                len(completed) + len(ready_tasks),
                total_tasks,
                details=f"batch {batch_no} finished ok={succeeded} failed={failed}",
            )
            if failed:
                failed_ids = ", ".join(r.task_id[:8] for r in results if r.status != TaskStatus.DONE)
                self.console.emit("ERROR", f"Batch {batch_no}: failed task(s): {failed_ids}")

            if any(r.status != TaskStatus.DONE for r in results):
                merged = self.merger.merge(final_results)
                module_state = self.module_state()
                return {"status": "failed", "merged": merged, "results": [r.as_dict() for r in final_results], "metrics": self.metrics.snapshot(), "console": self.console.events, "live_trace": self.live_trace_rows, "scheduler": [decision.as_dict() for decision in self.scheduler.decisions], "kernel_modules": self.module_manager.loaded_modules(), "module_state": module_state, "ai_activity": module_state.get("ai_activity", {}), "model_usage": module_state.get("model_usage", {}), "model_availability": module_state.get("model_availability", {}), "local_model_manager": module_state.get("local_model_manager", {})}

            for task in ready_tasks:
                completed.add(task.task_id)
                pending.pop(task.task_id)

        merged = self.merger.merge(final_results)
        self.console.progress("Parallel batches", total_tasks, total_tasks, details="orchestration complete")
        self.console.emit("DONE", "Все критерии выполнены (Асинхронный параллельный режим)")
        module_state = self.module_state()
        return {"status": "done", "merged": merged, "results": [r.as_dict() for r in final_results], "metrics": self.metrics.snapshot(), "console": self.console.events, "live_trace": self.live_trace_rows, "disabled_agents": self.autoscaler.disabled_agents, "enabled_agents": self.autoscaler.enabled_agents, "scheduler": [decision.as_dict() for decision in self.scheduler.decisions], "kernel_modules": self.module_manager.loaded_modules(), "module_state": module_state, "ai_activity": module_state.get("ai_activity", {}), "model_usage": module_state.get("model_usage", {}), "model_availability": module_state.get("model_availability", {}), "local_model_manager": module_state.get("local_model_manager", {})}

    async def run_async(self, root_task: Task) -> dict:
        """Asynchronous entry point that leverages parallel execution."""
        self.live_trace_rows = []
        self.console.emit("AGENTS", f"Найдено агентов: {len(self.registry.list_agents())}, доступно: {len(self.registry.ready_agents())}")
        self.healthcheck.check_all()
        
        plan = self.create_execution_plan(root_task)
        return await self.run_plan_parallel(plan)

    async def run(self, root_task: Task) -> dict:
        """Asynchronous public entry point for orchestrated task execution."""
        return await self.run_async(root_task)

    def run_sync(self, root_task: Task) -> dict:
        """Synchronous wrapper for callers that cannot await run()."""
        try:
            loop = asyncio.get_running_loop()
            if loop.is_running():
                raise RuntimeError("run_sync() cannot be called from a running event loop; use await run()")
        except RuntimeError as exc:
            if "no running event loop" not in str(exc).lower():
                raise
        return asyncio.run(self.run(root_task))

    async def listen_for_tasks(self):
        from .task_listener import TaskListener
        if self._training_consolidation_task is None or self._training_consolidation_task.done():
            self._training_consolidation_stop.clear()
            self._training_consolidation_task = asyncio.create_task(self._training_consolidation_loop())
        if self._kpi_dashboard_task is None or self._kpi_dashboard_task.done():
            self._kpi_dashboard_stop.clear()
            try:
                self._refresh_kpi_dashboard()
            except Exception as exc:
                self.log("warning", f"[KPI] initial dashboard refresh failed: {exc}")
            self._kpi_dashboard_task = asyncio.create_task(self._kpi_dashboard_loop())
        if self._provider_inventory_task is None or self._provider_inventory_task.done():
            self._provider_inventory_stop.clear()
            try:
                self._refresh_provider_inventory_snapshot(force_refresh=True)
            except Exception as exc:
                self.log("warning", f"[INVENTORY] initial provider refresh failed: {exc}")
            self._provider_inventory_task = asyncio.create_task(self._provider_inventory_loop())
        listener = TaskListener(self)
        try:
            await listener.start()
        finally:
            self._training_consolidation_stop.set()
            self._kpi_dashboard_stop.set()
            self._provider_inventory_stop.set()
            if self._training_consolidation_task is not None:
                self._training_consolidation_task.cancel()
                try:
                    await self._training_consolidation_task
                except asyncio.CancelledError:
                    pass
                except Exception:
                    pass
            if self._kpi_dashboard_task is not None:
                self._kpi_dashboard_task.cancel()
                try:
                    await self._kpi_dashboard_task
                except asyncio.CancelledError:
                    pass
                except Exception:
                    pass
            if self._provider_inventory_task is not None:
                self._provider_inventory_task.cancel()
                try:
                    await self._provider_inventory_task
                except asyncio.CancelledError:
                    pass
                except Exception:
                    pass
