from __future__ import annotations
import asyncio
import hashlib
import json
import os
import threading
import time
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any
from datetime import UTC, datetime

from core.agents.base_agent import BaseAgent

from .agent_factory import AgentFactory
from .agent_registry import AgentRegistry
from .feedback_loop import FeedbackLoop
from .healthcheck import HealthChecker
from .host_bridge import HostBridge
from .kpi import KPIEvaluator
from .load_balancer import LoadBalancer, is_agent_routable
from .metrics import MetricsCollector
from .message_bus import MessageBus
from .model_selector import ModelSelector
from .models import AgentResult, AgentStatus, ExecutionPlan, Priority, Task, TaskAcceptance, TaskStatus
from .orchestration_config import OrchestrationConfig
from .quality_analyzer import QualityAnalyzer
from .security_gate import SecurityGate
from .result_merger import ResultMerger
from .smart_scheduler import SmartScheduler
from .session_memory import MemoryScope, SessionMemory
from .availability import ModelAvailability, ModelAvailabilityModule, ProviderStatus
from .ai_activity_module import AIActivityModule
from .data_plane_monitor import build_data_plane_snapshot
from .antigravity_status_module import AntigravityStatusModule
from .api_bridge_module import APIBridgeModule
from .smart_decomposer_module import SmartDecomposerModule
from .prompt_optimizer_module import PromptOptimizerModule
from .chat_bus import ChatBusModule
from .trigger_dispatcher import TriggerDispatcherModule
from .json_themes_module import JSONThemesModule
from .unified_vfs import UnifiedVFSModule
from .kernel_module_manager import KernelModuleManager
from .orchestrator_control_module import OrchestratorControlModule
from .model_usage_module import ModelUsageModule
from .provider_budget_router import ProviderBudgetRouter
from .cold_boot_module import ColdBootModule
from .voice_listener_module import VoiceListenerModule
from .kpi_event_logger import KPIEventLogger
from .effectiveness_dashboard import build_kpi_dashboard
from .ui_design_system_module import UIDesignSystemModule
from .ui_anti_template_module import UIAntiTemplateModule
from .frontend_engineering_bridge_module import FrontendEngineeringBridgeModule
from .autodev_pipeline_module import AutodevPipelineModule
from .tdd_policy_module import StrictTDDModule
from .qwen_code_module import QwenCodeModule
from .code_readability_module import CodeReadabilityModule
from .dev_toolkit_module import DevToolkitModule
from .dependency_manager import DependencyManager
from .self_diagnostic_module import SelfDiagnosticModule
from ..mimo.proxy import MimoOrchestrationDirector



from .local_llm_bridge import LocalLLMBridge
from .local_llm_module import LocalLLMModule
from .sourcecraft_module import SourceCraftModule
from .reasoning_module import ReasoningModule
from .risk_advisor_module import RiskAdvisorModule
from .orchestrator_advisor_module import OrchestratorAdvisorModule
from .intelligence_module import AIIntelligenceModule
from .security_sentinel import KernelSecuritySentinel


TIMEOUT_ERROR_TYPES = {"tcp_timeout", "api_timeout", "sdk_hang"}
from .task_decomposer import TaskDecomposer
from .task_router import CAPABILITY_BY_TASK_TYPE, TaskRouter
from .user_console import UserConsole


class Orchestrator:
    def get_context(self, key: str) -> Any:
        return getattr(self, key, None)

    def emit_event(self, event_name: str, payload: dict[str, Any]) -> None:
        self.console.emit(event_name, str(payload))

    def module_state(self) -> dict[str, Any]:
        return self.module_manager.finalize()

    def query_state(self, module_name: str, key: str) -> Any:
        return self.module_state().get(module_name, {}).get(key)

    def query_module_state(self, module_name: str, key: str) -> Any:
        return self.query_state(module_name, key)

    def get_memory(self) -> SessionMemory:
        return self.session_memory

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
    def _testing_mode() -> bool:
        return os.getenv("TESTING", "").strip().lower() == "true" or bool(os.getenv("PYTEST_CURRENT_TEST"))

    def _antigravity_status_snapshot(self) -> dict[str, Any]:
        module = self.module_manager.get_module("antigravity_status")
        if module and hasattr(module, "snapshot"):
            snapshot = module.snapshot()
            return snapshot if isinstance(snapshot, dict) else {"value": snapshot}
        return {}

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

    def __init__(self, registry: AgentRegistry | None = None, retry_limit: int = 3, idle_shutdown_sec: int = 900) -> None:
        self.local_agents: dict[str, BaseAgent] = {}
        self.results: dict[str, AgentResult] = {}
        self.live_trace_rows: list[dict[str, object]] = []
        
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
        if hasattr(self.healthcheck, "set_module_state_source"):
            self.healthcheck.set_module_state_source(self.module_state)
        self.availability = ModelAvailability()
        self.feedback = components.feedback
        self.metrics = components.metrics
        self.kpi = components.kpi
        self.kpi.task_thresholds = dict(self.orchestration_config.kpi_thresholds_by_task)
        self.quality = components.quality
        self.merger = components.merger
        self.console = components.console
        self.security_gate = components.security_gate
        self.host_bridge = components.host_bridge
        self.session_memory = components.session_memory
        self.memory_consolidator = components.memory_consolidator
        self.provider_budget_router = ProviderBudgetRouter()
        self.kpi_events = KPIEventLogger.from_env()
        if getattr(self.orchestration_config, "kpi_rejection_summary_path", ""):
            self.kpi_events.summary_path = Path(self.orchestration_config.kpi_rejection_summary_path)
        self._postgres_watchdog_stop = threading.Event()
        self._postgres_watchdog_thread: threading.Thread | None = None
        self._training_consolidation_stop = threading.Event()
        self._kpi_dashboard_stop = threading.Event()
        self._training_consolidation_lock = threading.Lock()
        self._training_consolidation_queue: list[dict[str, Any]] = []
        self._training_consolidation_task: asyncio.Task[None] | None = None
        self._kpi_dashboard_task: asyncio.Task[None] | None = None
        self._training_consolidation_interval_sec = max(60, int(self.orchestration_config.training_consolidation_interval_sec))
        self._kpi_dashboard_interval_sec = max(300, int(getattr(self.orchestration_config, "kpi_dashboard_interval_sec", 3600)))
        self.local_llm_bridge = LocalLLMBridge(host_bridge=self.host_bridge)
        self.mimo_director = MimoOrchestrationDirector()
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
        self.module_manager.register(ModelUsageModule())
        self.module_manager.register(ModelAvailabilityModule())
        self.module_manager.register(AntigravityStatusModule())
        self.module_manager.register(APIBridgeModule())
        self.mimo_director.set_status_source(self._antigravity_status_snapshot)
        self.module_manager.register(SmartDecomposerModule())
        self.module_manager.register(PromptOptimizerModule())
        self.module_manager.register(ChatBusModule())
        self.module_manager.register(TriggerDispatcherModule())
        self.module_manager.register(JSONThemesModule())
        self.module_manager.register(UnifiedVFSModule())
        self.module_manager.register(ColdBootModule())
        self.module_manager.register(UIDesignSystemModule())
        self.module_manager.register(UIAntiTemplateModule())
        self.module_manager.register(FrontendEngineeringBridgeModule())
        self.module_manager.register(AutodevPipelineModule())
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
        
        # Register DesignConceptAgent
        from core.agents.design_concept_agent import DesignConceptAgent
        design_agent = DesignConceptAgent()
        self.attach_local_agent(
            agent_id="design_concept_agent",
            agent=design_agent,
            agent_type="custom"
        )
        design_agent.set_host_bridge(self)
        
        self.module_manager.load("ai_activity")
        self.module_manager.load("orchestrator_control")
        self.module_manager.load("model_usage")
        self.mimo_director.set_budget_module(self.module_manager.get_module("model_usage"))
        self.module_manager.load("unified_vfs")
        self.mimo_director.set_vfs_source(self.module_manager.get_module("unified_vfs"))
        self.mimo_director.safe_sync()
        self.module_manager.load("model_availability")
        self.module_manager.load("api_bridge")
        self.module_manager.load("smart_decomposer")
        self.module_manager.load("prompt_optimizer")
        self.module_manager.load("chat_bus")
        self.module_manager.load("trigger_dispatcher")
        self.module_manager.load("json_themes")
        self.module_manager.load("cold_boot")
        self.module_manager.load("ui_design_system")
        self.module_manager.load("ui_anti_template")
        self.module_manager.load("frontend_engineering_bridge")
        self.module_manager.load("autodev_pipeline")
        self.module_manager.load("tdd_policy")
        self.module_manager.load("qwen_code")
        self.module_manager.load("readability_policy")
        self.module_manager.load("dev_toolkit")
        self.module_manager.load("self_diagnostic")



        self.module_manager.load("sourcecraft")
        self.module_manager.load("voice_listener")
        self.module_manager.load("reasoning")
        self.module_manager.load("risk_advisor")
        self.module_manager.load("orchestrator_advisor")
        self.module_manager.load("intelligence")
        self.module_manager.load("security_sentinel")

        # Load local_llm before autostart so the module is available for
        # advisory context and readiness checks during kernel boot.
        if not self._testing_mode():
            self.module_manager.load("local_llm")
        self._autostart_local_llm()
        self._autostart_easy_diffusion()
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
                        "tables": [item.__dict__ for item in snapshot.tables],
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
        payload = {
            "session_id": task.session_id or task.task_id,
            "agent_id": result.agent_id or "orchestrator",
            "task_type": task.type.value,
            "memory_domain": memory_domain,
            "summary": str(result.output.get("summary", "") or "").strip(),
            "source_memory_ids": [],
            "quality_score": max(0.0, min(1.0, float(result.confidence))),
            "metadata": {
                "task_id": task.task_id,
                "status": result.status.value,
                "memory_domain": memory_domain,
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
        return processed

    async def _training_consolidation_loop(self) -> None:
        while not self._training_consolidation_stop.is_set():
            await asyncio.sleep(self._training_consolidation_interval_sec)
            self._flush_training_consolidation_queue()

    def _refresh_kpi_dashboard(self) -> dict[str, Any]:
        kpi_log = Path(getattr(self.kpi_events, "file_path", "memory_store/kpi_events.jsonl"))
        rolling = Path("core/mimo/profiles/rolling_kpi_store.json")
        summary = Path(getattr(self.orchestration_config, "kpi_dashboard_output_path", "memory_store/kpi_dashboard_24h.json") or "memory_store/kpi_dashboard_24h.json")
        dashboard = build_kpi_dashboard(kpi_log_path=kpi_log, rolling_kpi_path=rolling, summary_path=summary)
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

    def _init_original(self, registry: AgentRegistry | None = None, retry_limit: int = 3, idle_shutdown_sec: int = 900) -> None:
        self.local_agents = {}
        self.results = {}
        self.live_trace_rows = []

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
        self.availability = ModelAvailability()
        self.feedback = components.feedback
        self.metrics = components.metrics
        self.kpi = components.kpi
        self.kpi.task_thresholds = dict(self.orchestration_config.kpi_thresholds_by_task)
        self.quality = components.quality
        self.merger = components.merger
        self.console = components.console
        self.security_gate = components.security_gate
        self.host_bridge = components.host_bridge
        self.session_memory = components.session_memory
        self.provider_budget_router = ProviderBudgetRouter()
        self.kpi_events = KPIEventLogger.from_env()
        if getattr(self.orchestration_config, "kpi_rejection_summary_path", ""):
            self.kpi_events.summary_path = Path(self.orchestration_config.kpi_rejection_summary_path)
        self._postgres_watchdog_stop = threading.Event()
        self._postgres_watchdog_thread: threading.Thread | None = None

        self.module_manager = KernelModuleManager()
        self.module_manager.set_api(self)
        self.module_manager.register(AIActivityModule())
        self.module_manager.register(OrchestratorControlModule())
        self.module_manager.register(ModelUsageModule())
        self.module_manager.register(ModelAvailabilityModule())
        self.module_manager.register(AntigravityStatusModule())
        self.module_manager.register(APIBridgeModule())
        self.mimo_director.set_status_source(lambda: self.module_manager.get_module("antigravity_status").snapshot() if self.module_manager.is_loaded("antigravity_status") and hasattr(self.module_manager.get_module("antigravity_status"), "snapshot") else {})
        self.module_manager.register(SmartDecomposerModule())
        self.module_manager.register(PromptOptimizerModule())
        self.module_manager.register(ChatBusModule())
        self.module_manager.register(TriggerDispatcherModule())
        self.module_manager.register(ColdBootModule())
        self.module_manager.register(SourceCraftModule())
        self.module_manager.register(VoiceListenerModule())
        self.module_manager.load("ai_activity")
        self.module_manager.load("orchestrator_control")
        self.module_manager.load("model_usage")
        self.module_manager.load("model_availability")
        self.module_manager.load("api_bridge")
        self.module_manager.load("smart_decomposer")
        self.module_manager.load("prompt_optimizer")
        self.module_manager.load("chat_bus")
        self.module_manager.load("trigger_dispatcher")
        self.module_manager.load("cold_boot")
        self.module_manager.load("sourcecraft")
        self.module_manager.load("voice_listener")
        
    def attach_local_agent(self, agent_id: str, agent: BaseAgent, agent_type: str = "custom", critical: bool = False, model_name: str = "local-small", provider: str = "local") -> None:
        self.local_agents[agent_id] = agent
        agent.set_host_bridge(self.host_bridge)
        if not self.registry.get(agent_id):
            self.registry.register(agent_id, agent_type, f"local://{agent_id}", agent.capabilities, critical=critical, model_name=model_name, provider=provider)
            self.metrics.register_agent(self.registry.get(agent_id))  # type: ignore[arg-type]
            
        # 2. Register as TPP Pod (Mesh Architecture)
        if hasattr(self.message_bus, "register_pod"):
            self.message_bus.register_pod(agent_id, agent.capabilities)
        self.log("info", f"[KERNEL] Attached local agent pod: {agent_id} (TPP Enabled)")

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
        yield {"type": "stream_event", "stage": "ACCEPTED", "message": "task accepted by orchestrator"}

        try:
            while not task_future.done():
                try:
                    yield await asyncio.wait_for(event_queue.get(), timeout=0.25)
                except asyncio.TimeoutError:
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

    def run_autodev_pipeline(self, specs: str, project_root: str = ".", figma_api_available: bool = False) -> dict[str, object]:
        module = self.module_manager.get_module("autodev_pipeline")
        if not isinstance(module, AutodevPipelineModule):
            raise RuntimeError("autodev_pipeline module is not loaded")
        return module.run_pipeline(specs=specs, project_root=project_root, figma_api_available=figma_api_available)

    def monitoring_snapshot(self) -> dict[str, object]:
        control = self._control_module()
        if control is None:
            return {"source_of_truth": "orchestrator", "status": "control_module_not_loaded"}
        return control.finalize()

    @staticmethod
    def _normalize_provider(provider: str) -> str:
        p = provider.strip().lower()
        if p in {"antigravity", "antigravity-cli", "agy"}:
            return "antigravity"
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

    def _build_decomposition_advisory(self, task: Task) -> dict[str, object]:
        advisory_context: dict[str, object] = {}

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
            except Exception:
                advisory_context["sourcecraft"] = {"enabled": False, "should_delegate": False}

        local_llm_module = self.module_manager.get_module("local_llm") if hasattr(self.module_manager, "get_module") else None
        if local_llm_module and isinstance(local_llm_module, LocalLLMModule):
            try:
                # Use build_decomposition_draft for the "First Layer" planning
                advisory_context["local_llm"] = local_llm_module.build_decomposition_draft(
                    task,
                    {
                        "description": task.input.description,
                        "repo_path": task.context.repo_path,
                        "branch": task.context.branch,
                    },
                )
                self.log("info", f"[LOCAL_LLM] First-layer decomposition draft generated for task {task.task_id}")
            except Exception as e:
                self.log("warning", f"[LOCAL_LLM] Failed to generate decomposition draft: {e}")
                advisory_context["local_llm"] = {"enabled": False, "ready": False, "should_delegate": False}

        return advisory_context

    def create_execution_plan(self, task: Task) -> ExecutionPlan:
        self.console.emit("PLAN", "Задача проанализирована")
        advisory_context = self._build_decomposition_advisory(task)

        # Try smart decomposition first (Higher level AI/Reasoning)
        smart_decomp = self.module_manager.get_module("smart_decomposer")
        if isinstance(smart_decomp, SmartDecomposerModule):
            try:
                plan = smart_decomp.decompose_task(task)
                if plan:
                    self.console.emit("PLAN", f"Умная декомпозиция (Reasoning): создано {len(plan.atomic_tasks)} задач")
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

        return plan

    def _load_memory_context(self, task: Task, agent_id: str) -> dict[str, object]:
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
        local_llm_context.update(selection_context)
        local_llm_context.setdefault("ready", True)
        local_llm_context.setdefault("should_delegate", True)
        local_llm_context.setdefault("task_family", "mimo")
        advisory_context["local_llm"] = local_llm_context
        advisory_context["mimo"] = selection_context

        choice = self.model_selector.select(task, advisory_context=advisory_context)
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

        module_context: dict[str, object] = {
            "selected_provider": choice.provider,
            "selected_model": choice.model_name,
            "reason": choice.reason,
            **advisory_context,
        }
        self.module_manager.before_task(task, module_context)

        self.autoscaler.ensure_capacity(capability)
        decision = self.scheduler.schedule(task)
        if decision.requires_orchestrator:
            self.console.emit("SCHEDULER", f"Orchestrator route: {decision.reason}")
        else:
            self.console.emit("SCHEDULER", f"P2P route allowed: {decision.reason}")

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

        self.console.emit(
            "ROUTING",
            f"task_id={task.task_id} router_agent={agent_id} router_provider={agent_record.provider if agent_record else '-'} "
            f"fallback={fallback} secondary_review={choice.requires_secondary_review}",
        )

        # Pre-flight provider diagnostics: verify DNS/TCP/API/model readiness before spending a task attempt.
        provider = self._normalize_provider(agent_record.provider if agent_record else choice.provider)
        preflight_live = os.getenv("AI_BRIDGE_PREFLIGHT_LIVE_PROBE", "true").strip().lower() in {"1", "true", "yes", "on"}
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
            memory_context = self._load_memory_context(task, agent_id)
            
            # TPP: Mark pod as BUSY
            self._broadcast_pod_state(agent_id, AgentStatus.BUSY, task_id=task.task_id)
            
            result = agent.run(task, memory_context=memory_context)
            
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
                if classified:
                    self.provider_budget_router.mark_failure(task, source_provider, classified)
                    self.availability.record_failure(source_provider, classified, result_errors)

                # Proactive Soft Fallback for all critical/high failures or quota issues
                should_fallback = (
                    classified in {"quota_exhaustion", "auth_fail", "api_timeout", "tcp_timeout"}
                    or (is_google_cli and classified in TIMEOUT_ERROR_TYPES)
                    or (task.priority in {Priority.HIGH, Priority.CRITICAL} and result.status == TaskStatus.FAILED)
                )

                if should_fallback:
                    fallback_chain = self.provider_budget_router.preferred_providers(task, choice)
                    # Exclude the failed agent
                    fallback_agent_id = self._select_agent_by_provider_preference(capability, fallback_chain, exclude={agent_id}, priority=task.priority)
                    if fallback_agent_id:
                        self.console.emit("FALLBACK", f"task_id={task.task_id} from={agent_id} to={fallback_agent_id} reason={classified or 'failure'}")
                        fallback_count += 1
                        fallback_agent = self.local_agents.get(fallback_agent_id)
                        if fallback_agent:
                            result = fallback_agent.run(task, memory_context=memory_context)
                            # Classify again for metrics
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
            if isinstance(history, list) and history:
                for item in reversed(history):
                    if isinstance(item, dict) and item.get("task_id") == task.task_id:
                        tokens_used = item.get("tokens_used")
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
                "errors_count": len(result.errors or []),
            }
            self.kpi_events.write(lifecycle_payload)
            self.kpi_events.append_fallback(lifecycle_payload)
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
        
        while pending:
            ready_tasks = [task for task in pending.values() if all(dep in completed for dep in task.dependencies)]
            if not ready_tasks:
                raise RuntimeError("Task graph has unresolved dependencies or cycles")
                
            usage_module = self._model_usage_module()
            if usage_module is not None and usage_module.should_reduce_parallelism() and len(ready_tasks) > 1:
                self.console.emit("THROTTLE", f"Token budget is low; reducing parallel batch from {len(ready_tasks)} to 1")
                ready_tasks = ready_tasks[:1]

            self.console.emit("PARALLEL", f"Запуск {len(ready_tasks)} задач параллельно...")

            results = await asyncio.gather(*(self.run_task_async(t) for t in ready_tasks))
            
            final_results.extend(results)
            
            if any(r.status != TaskStatus.DONE for r in results):
                merged = self.merger.merge(final_results)
                module_state = self.module_state()
                return {"status": "failed", "merged": merged, "results": [r.as_dict() for r in final_results], "metrics": self.metrics.snapshot(), "console": self.console.events, "live_trace": self.live_trace_rows, "scheduler": [decision.as_dict() for decision in self.scheduler.decisions], "kernel_modules": self.module_manager.loaded_modules(), "module_state": module_state, "ai_activity": module_state.get("ai_activity", {}), "model_usage": module_state.get("model_usage", {}), "model_availability": module_state.get("model_availability", {})}
                
            for task in ready_tasks:
                completed.add(task.task_id)
                pending.pop(task.task_id)

        merged = self.merger.merge(final_results)
        self.console.emit("DONE", "Все критерии выполнены (Асинхронный параллельный режим)")
        module_state = self.module_state()
        return {"status": "done", "merged": merged, "results": [r.as_dict() for r in final_results], "metrics": self.metrics.snapshot(), "console": self.console.events, "live_trace": self.live_trace_rows, "disabled_agents": self.autoscaler.disabled_agents, "enabled_agents": self.autoscaler.enabled_agents, "scheduler": [decision.as_dict() for decision in self.scheduler.decisions], "kernel_modules": self.module_manager.loaded_modules(), "module_state": module_state, "ai_activity": module_state.get("ai_activity", {}), "model_usage": module_state.get("model_usage", {}), "model_availability": module_state.get("model_availability", {})}

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
        listener = TaskListener(self)
        try:
            await listener.start()
        finally:
            self._training_consolidation_stop.set()
            self._kpi_dashboard_stop.set()
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
