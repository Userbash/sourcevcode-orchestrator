from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any

from .kernel_protocol import KernelAPI, KernelModule
from .local_model_runtime import LocalModelRuntime, LocalModelRuntimeConfig, LocalPromptBuilder

logger = logging.getLogger("local_llm_module")

HIGH_RISK_KEYWORDS = (
    "security",
    "auth",
    "rbac",
    "payment",
    "secret",
    "production",
    "migration",
    "destructive",
)
LOCAL_LLM_TASK_KEYWORDS = {
    "repo_ops": ("repo", "repository", "worktree", "branch", "status", "diff", "clone", "checkout"),
    "docs_workflow": ("docs", "documentation", "summary", "explain", "commit message", "commit log"),
    "verification": ("test", "tests", "ci", "verification", "checklist", "health", "workflow"),
    "planning": ("plan", "plan:", "break down", "decompose", "roadmap"),
    "analysis": ("research", "review", "analysis", "compare", "investigate"),
}


class LocalLLMModule(KernelModule):
    def __init__(
        self,
        endpoint: str | None = None,
        model_name: str | None = None,
        timeout_sec: float | None = None,
    ) -> None:
        self.name = "local_llm"
        self._api: KernelAPI | None = None
        self.runtime = LocalModelRuntime(
            LocalModelRuntimeConfig.from_env(
                endpoint=endpoint,
                model_name=model_name,
                timeout_sec=timeout_sec,
            )
        )
        self.endpoint = self.runtime.current_endpoint
        self.model_name = self.runtime.config.default_model
        self.timeout_sec = self.runtime.config.health_timeout_sec
        self.generate_timeout_sec = self.runtime.config.generate_timeout_sec
        self.last_probe: dict[str, Any] = {}
        self.last_advisory: dict[str, Any] = {}
        self.last_query_metrics: dict[str, Any] = {}
        self.adapter_state_path = Path(os.getenv("AI_BRIDGE_EXPERIENCE_ADAPTER_STATE_PATH", "memory_store/training/experience_adapter_state.json"))
        self._adapter_state_mtime_ns: int | None = None
        self._adapter_state_cache: dict[str, Any] = {}

    def _refresh_runtime_model(self, model_name: str | None = None) -> str:
        target_model = (model_name or self.model_name).strip() or self.runtime.config.default_model
        if target_model != self.runtime.config.default_model:
            self.runtime.config.default_model = target_model
        self.model_name = target_model
        return target_model

    def can_use_model(self, model_name: str | None = None) -> dict[str, Any]:
        target_model = self._refresh_runtime_model(model_name)
        probe = self._probe(target_model)
        model_present = bool(probe.get("model_present"))
        return {
            "ok": bool(probe.get("ok")) and model_present,
            "service_reachable": bool(probe.get("ok")),
            "model_present": model_present,
            "model_name": target_model,
            "status_code": probe.get("status_code"),
            "available_models": probe.get("available_models", []),
            "error": probe.get("error"),
        }

    @staticmethod
    def _model_matches(expected: str, candidate: str) -> bool:
        return LocalModelRuntime._model_matches(expected, candidate)

    @staticmethod
    def _task_text(task: Any, context: dict[str, Any] | None = None) -> str:
        pieces: list[str] = []
        if context:
            for key in ("description", "objective", "message", "prompt", "summary"):
                value = context.get(key)
                if isinstance(value, str) and value.strip():
                    pieces.append(value.strip())
        description = str(getattr(getattr(task, "input", None), "description", "") or "").strip()
        if description:
            pieces.append(description)
        task_type = str(getattr(getattr(task, "type", None), "value", getattr(task, "type", ""))).strip()
        if task_type:
            pieces.append(task_type)
        files = getattr(getattr(task, "input", None), "files", []) or []
        if isinstance(files, list):
            pieces.extend(str(item) for item in files if str(item).strip())
        constraints = getattr(getattr(task, "input", None), "constraints", []) or []
        if isinstance(constraints, list):
            pieces.extend(str(item) for item in constraints if str(item).strip())
        return " ".join(pieces).lower()

    @staticmethod
    def _task_family(task_text: str) -> str:
        for family, keywords in LOCAL_LLM_TASK_KEYWORDS.items():
            if any(keyword in task_text for keyword in keywords):
                return family
        return "general"

    @staticmethod
    def _high_risk(task_text: str) -> bool:
        return any(keyword in task_text for keyword in HIGH_RISK_KEYWORDS)

    def _load_adapter_state(self) -> dict[str, Any]:
        try:
            stat = self.adapter_state_path.stat()
        except FileNotFoundError:
            self._adapter_state_mtime_ns = None
            self._adapter_state_cache = {}
            return {}
        except Exception:
            return self._adapter_state_cache

        if self._adapter_state_mtime_ns == stat.st_mtime_ns and self._adapter_state_cache:
            return self._adapter_state_cache

        try:
            payload = json.loads(self.adapter_state_path.read_text(encoding='utf-8'))
        except Exception:
            return self._adapter_state_cache
        if not isinstance(payload, dict):
            return self._adapter_state_cache
        self._adapter_state_mtime_ns = stat.st_mtime_ns
        self._adapter_state_cache = payload
        return payload

    def _training_profile(self, task_type: str | None, task_family: str) -> dict[str, Any]:
        payload = self._load_adapter_state()
        profiles = payload.get('task_profiles', {}) if isinstance(payload, dict) else {}
        if not isinstance(profiles, dict):
            return {}
        if task_type:
            profile = profiles.get(task_type)
            if isinstance(profile, dict):
                return profile
        for profile in profiles.values():
            if isinstance(profile, dict) and str(profile.get('task_family') or '') == task_family:
                return profile
        return {}

    @staticmethod
    def _recommended_actions(task_family: str) -> list[str]:
        mapping = {
            "repo_ops": [
                "summarize the worktree and recent repository changes",
                "prepare a concise handoff for the orchestrator",
                "highlight immediate repo actions without mutating state",
            ],
            "docs_workflow": [
                "draft the documentation or commit text",
                "compress the change into a readable summary",
                "surface the outcome for the reviewer and orchestrator",
            ],
            "verification": [
                "prepare a test plan or checklist",
                "summarize verification steps for the core",
                "highlight likely failure points before execution",
            ],
            "planning": [
                "break the task into smaller steps",
                "produce a lightweight execution outline",
                "identify which parts remain in the core",
            ],
            "analysis": [
                "summarize the options and tradeoffs",
                "prepare a comparison of likely approaches",
                "extract the useful context for the next agent",
            ],
        }
        return mapping.get(task_family, [
            "summarize the task",
            "compress context for the core",
        ])

    @staticmethod
    def _core_retained_actions() -> list[str]:
        return [
            "security enforcement",
            "provider routing",
            "scheduler decisions",
            "budget controls",
            "mutating execution",
            "failover and retries",
        ]

    @staticmethod
    def _safe_offload_actions() -> dict[str, list[str]]:
        return {
            "full_offload": ["docs_workflow", "analysis"],
            "partial_offload": ["planning", "verification", "review"],
            "core_only": ["security", "auth", "destructive", "migration", "sourcecraft"],
        }

    def build_offload_profile(self, task: Any, context: dict[str, Any] | None = None) -> dict[str, Any]:
        advisory = self._advisory_base(task, context)
        family = str(advisory.get("task_family") or "general")
        should_delegate = bool(advisory.get("should_delegate"))
        offload = self._safe_offload_actions()
        can_offload_fully = should_delegate and family in set(offload["full_offload"])
        can_offload_partially = should_delegate or family in set(offload["partial_offload"])
        return {
            **advisory,
            "offload": {
                "can_offload_fully": can_offload_fully,
                "can_offload_partially": can_offload_partially,
                "full_offload": offload["full_offload"],
                "partial_offload": offload["partial_offload"],
                "core_only": offload["core_only"],
                "recommended_boundary": "local_llm" if can_offload_partially else "core",
            },
        }

    def _probe(self, model_name: str | None = None) -> dict[str, Any]:
        health = self.runtime.check_health_sync(model_name or self.model_name)
        self.endpoint = health.endpoint
        return health.as_dict()

    def _query_model(
        self,
        prompt: str,
        model_name: str | None = None,
        *,
        system: str | None = None,
        options: dict[str, Any] | None = None,
        timeout_sec: float | None = None,
    ) -> str:
        target_model = (model_name or self.model_name).strip()
        readiness = self.can_use_model(target_model)
        if not readiness["ok"]:
            raise RuntimeError(
                f"local LLM is not ready: service_reachable={readiness['service_reachable']}, model_present={readiness['model_present']} (endpoint={self.endpoint})"
            )

        result = self.runtime.generate_sync(
            prompt,
            target_model,
            system=system,
            options=options,
            timeout_sec=timeout_sec,
        )
        self.endpoint = result.endpoint
        self.last_query_metrics = {"provider": "local", "model": target_model, **result.metrics.as_dict()}
        return result.text

    def _advisory_base(self, task: Any, context: dict[str, Any] | None = None) -> dict[str, Any]:
        probe = self.can_use_model(self.model_name)
        ready = bool(probe.get("ok"))
        task_text = self._task_text(task, context)
        task_family = self._task_family(task_text)
        task_type = str(getattr(getattr(task, "type", None), "value", getattr(task, "type", ""))).lower() or None
        complexity = str(getattr(getattr(task, "complexity", None), "value", getattr(task, "complexity", "")) or "").lower() or None
        priority = str(getattr(getattr(task, "priority", None), "value", getattr(task, "priority", "")) or "").lower() or None
        high_risk = self._high_risk(task_text) or priority == "critical"
        should_delegate = ready and not high_risk and task_family in {"docs_workflow", "verification", "planning", "analysis"}
        preferred_model = self.model_name if ready else None
        training_profile = self._training_profile(task_type, task_family)
        learned_model = str(training_profile.get("preferred_model") or training_profile.get("recommended_model") or "").strip()
        if ready and learned_model:
            preferred_model = learned_model
        if ready and not high_risk and bool(training_profile.get("delegate")):
            should_delegate = True
        profile_weights = training_profile.get("profile_weights") if isinstance(training_profile.get("profile_weights"), dict) else {}
        context_depth = int(training_profile.get("context_depth") or 0)
        actions = self._recommended_actions(task_family)
        learned_practices = training_profile.get("best_practices") if isinstance(training_profile.get("best_practices"), list) else []
        if learned_practices:
            actions = list(dict.fromkeys(actions + [str(item) for item in learned_practices if str(item).strip()]))
        return {
            "enabled": ready,
            "ready": ready,
            "status": probe.get("status", "unknown") if isinstance(probe, dict) else "unknown",
            "endpoint": self.endpoint,
            "model_name": self.model_name,
            "task_family": task_family,
            "task_type": task_type,
            "priority": priority,
            "complexity": complexity,
            "high_risk": high_risk,
            "should_delegate": should_delegate,
            "recommended_owner": "local_llm" if should_delegate else "core",
            "preferred_model": preferred_model,
            "recommended_model": preferred_model,
            "context_depth": context_depth,
            "profile_weights": profile_weights,
            "training_profile": training_profile,
            "source_context": {
                "files": list(getattr(getattr(task, "input", None), "files", []) or []),
                "constraints": list(getattr(getattr(task, "input", None), "constraints", []) or []),
            },
            "actions": actions,
            "core_retained_actions": self._core_retained_actions(),
            "safe_offload": self._safe_offload_actions(),
            "summary": None,
            "task_text": task_text,
        }

    def _heuristic_decomposition_draft(self, task: Any, context: dict[str, Any] | None = None) -> dict[str, Any]:
        task_text = self._task_text(task, context)
        task_family = self._task_family(task_text)
        plan_layers = [
            {
                "name": "intake",
                "objective": "Normalize the request and extract constraints",
                "capability": "plan",
                "tasks": ["summarize the request", "list explicit constraints", "capture acceptance criteria"],
                "sub_agents": ["planner"],
                "dependencies": [],
            },
            {
                "name": "analysis",
                "objective": "Identify implementation surfaces and risks",
                "capability": "research",
                "tasks": ["identify affected modules", "list integration points", "flag risk areas"],
                "sub_agents": ["research", "review"],
                "dependencies": ["intake"],
            },
            {
                "name": "implementation",
                "objective": "Create implementation chunks for the core agents",
                "capability": "code",
                "tasks": ["backend changes", "frontend changes", "data changes"],
                "sub_agents": ["backend", "frontend", "database"],
                "dependencies": ["analysis"],
                "parallel_group": True,
            },
            {
                "name": "verification",
                "objective": "Prepare test and validation work",
                "capability": "test",
                "tasks": ["unit tests", "integration tests", "verification checklist"],
                "sub_agents": ["tester", "review"],
                "dependencies": ["implementation"],
            },
            {
                "name": "documentation",
                "objective": "Prepare the human-readable handoff",
                "capability": "docs",
                "tasks": ["update README", "write PR summary", "write commit summary"],
                "sub_agents": ["docs"],
                "dependencies": ["verification"],
            },
        ]
        if task_family == "repo_ops":
            plan_layers.insert(1, {
                "name": "repo_scan",
                "objective": "Inspect repository state and worktree changes",
                "capability": "docs",
                "tasks": ["repo status", "worktree diff", "changed files summary"],
                "sub_agents": ["sourcecraft"],
                "dependencies": ["intake"],
            })
        if task_family == "analysis":
            plan_layers[1]["tasks"] = ["compare approaches", "summarize tradeoffs", "identify risks"]
        return {
            "status": "heuristic",
            "task_family": task_family,
            "layers": plan_layers,
            "agent_map": {
                "planner": ["intake"],
                "research": ["analysis"],
                "backend": ["implementation"],
                "frontend": ["implementation"],
                "database": ["implementation"],
                "tester": ["verification"],
                "docs": ["documentation"],
                "sourcecraft": ["repo_scan"],
            },
            "sub_agents": ["planner", "research", "backend", "frontend", "database", "tester", "docs"],
        }

    def _parallel_strategy_prompt(self, task_text: str) -> str:
        return (
            "Return JSON only. You are a Senior Architect. Decompose this task into 3 independent parallel strategy drafts: "
            "1. Functional (focus on features), 2. Risk-Oriented (focus on safety), 3. Resource-Oriented (focus on speed/parallelism). "
            "Then synthesize them into a final 'layers' plan. "
            "Each layer must have: name, objective, capability, tasks (list), sub_agents (list), dependencies (list), parallel_group (bool). "
            "Task: " + task_text
        )

    def build_decomposition_draft(self, task: Any, context: dict[str, Any] | None = None) -> dict[str, Any]:
        advisory = self._advisory_base(task, context)
        if not advisory.get("ready"):
            advisory["decomposition"] = self._heuristic_decomposition_draft(task, context)
            return advisory

        task_text = advisory.get("task_text") or self._task_text(task, context)
        prompt = self._parallel_strategy_prompt(task_text)
        
        parsed: dict[str, Any] | None = None
        try:
            response = self.query(prompt, self.model_name)
            if response:
                try:
                    # Strip markdown markers if any
                    clean_response = response.strip()
                    if clean_response.startswith("```json"):
                        clean_response = clean_response[7:-3].strip()
                    elif clean_response.startswith("{"):
                        pass # already clean enough
                    
                    raw = json.loads(clean_response)
                    if isinstance(raw, dict):
                        parsed = raw
                except json.JSONDecodeError:
                    parsed = None
        except Exception as exc:
            advisory["decomposition_error"] = str(exc)

        if not parsed or "layers" not in parsed:
            parsed = self._heuristic_decomposition_draft(task, context)
            parsed["status"] = "heuristic"
        else:
            parsed.setdefault("status", "model")
            parsed.setdefault("task_family", advisory["task_family"])
            parsed.setdefault("sub_agents", [])
            parsed.setdefault("agent_map", {})
            parsed.setdefault("layers", [])

        advisory.update({
            "summary": parsed.get("summary") if isinstance(parsed.get("summary"), str) else advisory.get("summary"),
            "context_digest": parsed.get("context_digest") if isinstance(parsed.get("context_digest"), str) else None,
            "next_steps": parsed.get("next_steps") if isinstance(parsed.get("next_steps"), list) else advisory.get("actions", []),
            "model_hint": parsed.get("model_hint") if isinstance(parsed.get("model_hint"), str) else advisory.get("recommended_model"),
        })
        advisory["decomposition"] = parsed
        return advisory

    def build_advisory(self, task: Any, context: dict[str, Any] | None = None) -> dict[str, Any]:
        probe = self.check_health()
        ready = bool(probe.get("ok")) and bool(probe.get("model_present"))
        task_text = self._task_text(task, context)
        task_family = self._task_family(task_text)
        task_type = str(getattr(getattr(task, "type", None), "value", getattr(task, "type", ""))).lower() or None
        complexity = str(getattr(getattr(task, "complexity", None), "value", getattr(task, "complexity", "")) or "").lower() or None
        priority = str(getattr(getattr(task, "priority", None), "value", getattr(task, "priority", "")) or "").lower() or None
        high_risk = self._high_risk(task_text) or priority == "critical"
        should_delegate = ready and not high_risk and task_family in {"docs_workflow", "verification", "planning", "analysis"}
        preferred_model = self.model_name if ready else None
        advisory: dict[str, Any] = {
            "enabled": ready,
            "ready": ready,
            "status": probe.get("status", "unknown") if isinstance(probe, dict) else "unknown",
            "endpoint": self.endpoint,
            "model_name": self.model_name,
            "task_family": task_family,
            "task_type": task_type,
            "priority": priority,
            "complexity": complexity,
            "high_risk": high_risk,
            "should_delegate": should_delegate,
            "recommended_owner": "local_llm" if should_delegate else "core",
            "preferred_model": preferred_model,
            "recommended_model": preferred_model,
            "source_context": {
                "files": list(getattr(getattr(task, "input", None), "files", []) or []),
                "constraints": list(getattr(getattr(task, "input", None), "constraints", []) or []),
            },
            "actions": self._recommended_actions(task_family),
            "core_retained_actions": self._core_retained_actions(),
            "summary": None,
        }

        if should_delegate:
            prompt = LocalPromptBuilder.compose(
                "You are assisting an orchestrator. Return one short JSON object with keys summary, context_digest, next_steps, and model_hint. Keep it concise.",
                sections={
                    "task": task_text,
                    "task_family": task_family,
                    "recommended_actions": advisory["actions"],
                },
            )
            try:
                response = self.query(prompt, self.model_name)
                if response:
                    try:
                        parsed = json.loads(response)
                        if isinstance(parsed, dict):
                            advisory.update({
                                "summary": parsed.get("summary") if isinstance(parsed.get("summary"), str) else advisory.get("summary"),
                                "context_digest": parsed.get("context_digest") if isinstance(parsed.get("context_digest"), str) else None,
                                "next_steps": parsed.get("next_steps") if isinstance(parsed.get("next_steps"), list) else advisory["actions"],
                                "model_hint": parsed.get("model_hint") if isinstance(parsed.get("model_hint"), str) else preferred_model,
                            })
                    except json.JSONDecodeError:
                        advisory["summary"] = response[:240]
            except Exception as exc:
                advisory["summary"] = f"local_llm_unavailable: {exc}"

        self.last_advisory = advisory
        return advisory

    def _manager_module(self):
        if self._api is None or not hasattr(self._api, 'get_module'):
            return None
        try:
            return self._api.get_module('local_model_manager')
        except Exception:
            return None

    def pull_model(self, model_name: str | None = None) -> bool:
        """Seamlessly pulls the requested model from Ollama."""
        target_model = (model_name or self.model_name).strip()
        if self._api:
            self._api.log("info", f"[LOCAL_LLM] Pulling model {target_model}... This may take a while.")

        try:
            if self.runtime.pull_model_sync(target_model, timeout_sec=600.0):
                if self._api:
                    self._api.log("info", f"[LOCAL_LLM] Model {target_model} successfully PULLED.")
                return True
            if self._api:
                self._api.log("error", f"[LOCAL_LLM] Pull failed for model {target_model}.")
            return False
        except Exception as e:
            logger.error(f"Failed to pull local model {target_model}: {e}")
            return False

    def on_load(self, api: KernelAPI) -> None:
        self._api = api
        api.log("info", f"[LOCAL_LLM] Probing Ollama at {self.endpoint} for model {self.model_name}...")

        self.last_probe = self.check_health()
        
        if self.last_probe.get("ok") and self.last_probe.get("model_present"):
            api.log("info", f"[LOCAL_LLM] Local model {self.model_name} is reachable and ready.")
        elif self.last_probe.get("ok"):
            api.log("warning", f"[LOCAL_LLM] Ollama is reachable, but model {self.model_name} is not present. Startup provisioning is delegated to the bridge/runtime manager.")
        else:
            api.log("error", f"[LOCAL_LLM] Local model endpoint is unreachable: {self.last_probe.get('error', 'unknown error')}")


    def unload_model(self, model_name: str | None = None) -> bool:
        """Delegate unload lifecycle to local_model_manager when available."""
        target_model = (model_name or self.model_name).strip()
        manager = self._manager_module()
        try:
            if manager is not None and hasattr(manager, "unload_model"):
                ok = bool(manager.unload_model(target_model, reason="local_llm_unload"))
            else:
                ok = bool(self.runtime.unload_model_sync(target_model))
            if ok and self._api:
                self._api.log("info", f"[LOCAL_LLM] Model {target_model} successfully UNLOADED from VRAM.")
            return ok
        except Exception as e:
            logger.error(f"Failed to unload local model {target_model}: {e}")
            return False

    def hot_reload(self, new_model_name: str) -> bool:
        """Dynamically switch the active model while delegating residency to local_model_manager."""
        if self._api:
            self._api.log("info", f"[LOCAL_LLM] Hot-reloading model to {new_model_name}...")

        previous_model = self.model_name
        self.unload_model(previous_model)
        self._refresh_runtime_model(new_model_name)
        self.runtime.config.default_model = new_model_name

        self.last_probe = self.check_health()
        if not self.last_probe.get("model_present") and not self.pull_model():
            self._refresh_runtime_model(previous_model)
            self.runtime.config.default_model = previous_model
            return False

        manager = self._manager_module()
        if manager is not None and hasattr(manager, "warm_model"):
            try:
                manager.warm_model(new_model_name)
            except Exception as exc:
                logger.warning(f"Failed to warm local model via manager {new_model_name}: {exc}")

        if self._api:
            self._api.log("info", f"[LOCAL_LLM] Hot-reload successful. Active model: {self.model_name}")
        return True

    def on_unload(self) -> None:
        self.runtime.close()
        self.last_probe = {}
        self.last_advisory = {}

    def before_task(self, task: Any, context: dict[str, Any]) -> None:
        advisory = self.build_offload_profile(task, context)
        context["local_llm"] = advisory
        if advisory.get("should_delegate"):
            context["local_llm"]["automation"] = {
                "owner": "local_llm",
                "task_family": advisory.get("task_family"),
                "actions": advisory.get("actions", []),
                "core_retained_actions": advisory.get("core_retained_actions", []),
            }

    def after_task(self, task: Any, result: Any, context: dict[str, Any]) -> None:
        local_llm = context.get("local_llm")
        if not isinstance(local_llm, dict):
            return
        output = getattr(result, "output", {})
        summary = ""
        if isinstance(output, dict):
            summary = str(output.get("summary", "") or "")
        local_llm["last_result"] = {
            "task_id": getattr(task, "task_id", None),
            "status": getattr(getattr(result, "status", None), "value", getattr(result, "status", None)),
            "summary": summary,
        }

    @property
    def ready(self) -> bool:
        probe = self.last_probe or self.check_health()
        return bool(probe.get("ok")) and bool(probe.get("model_present"))

    def query(
        self,
        prompt: str,
        model_name: str | None = None,
        system: str | None = "You are a specialized AI Kernel Optimizer.",
        *,
        options: dict[str, Any] | None = None,
        timeout_sec: float | None = None,
    ) -> str:
        """Synchronous query for internal kernel tasks and local agent execution."""
        target_model = (model_name or self.model_name).strip()
        readiness = self.can_use_model(target_model)
        if not readiness.get("ok"):
            return ""
        try:
            return self._query_model(
                prompt,
                target_model,
                system=system,
                options=options,
                timeout_sec=timeout_sec,
            )
        except Exception as e:
            logger.error(f"Local LLM query failed: {e}")
            return ""

    def compact_memory(self, raw_data: list[dict[str, Any]]) -> str:
        """Uses local LLM to turn raw logs/history into a dense semantic summary."""
        prompt = f"Compact the following activity trace into a dense technical summary for another AI. Keep keys, schemas, and logic decisions. Data: {json.dumps(raw_data)}"
        return self.query(prompt, system="Summarize memory for context efficiency.")

    def generate_embedding_keywords(self, text: str) -> list[str]:
        """Generates semantic tags for indexing without calling expensive cloud APIs."""
        prompt = f"Return 10 technical keywords for indexing this code/task: {text[:1000]}"
        resp = self.query(prompt, system="Return only comma-separated keywords.")
        return [k.strip() for k in resp.split(",") if k.strip()]

    def analyze_p2p_intent(self, sender: str, receiver: str, payload: dict) -> bool:
        """Security: Analyze if a direct P2P message is safe and logical."""
        prompt = f"Analyze P2P message from {sender} to {receiver}. Payload: {json.dumps(payload)}. Is this safe and architecturaly sound? Return 'SAFE' or 'RISK: reason'."
        resp = self.query(prompt).upper()
        return "SAFE" in resp

    def check_health(self) -> dict[str, Any]:
        try:
            self.last_probe = self._probe()
        except Exception as exc:
            self.last_probe = {
                "ok": False,
                "status_code": None,
                "available_models": [],
                "model_present": False,
                "error": str(exc),
            }
        return self.last_probe

    def finalize(self) -> dict[str, Any]:
        probe = self.last_probe or self.check_health()
        ok = bool(probe.get("ok"))
        model_present = bool(probe.get("model_present"))
        if ok and model_present:
            status = "ready"
        elif ok:
            status = "degraded"
        else:
            status = "error"
        return {
            "status": status,
            "endpoint": self.endpoint,
            "model": self.model_name,
            "health_timeout_sec": self.timeout_sec,
            "service_reachable": ok,
            "model_present": model_present,
            "available_models": probe.get("available_models", []),
            "last_error": probe.get("error"),
            "advisory_examples": {
                "docs_workflow": self._recommended_actions("docs_workflow"),
                "verification": self._recommended_actions("verification"),
                "planning": self._recommended_actions("planning"),
                "analysis": self._recommended_actions("analysis"),
            },
            "last_advisory": self.last_advisory,
        }
