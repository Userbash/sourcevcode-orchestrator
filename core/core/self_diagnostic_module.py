from __future__ import annotations

import asyncio
import json
import logging
import os
from datetime import UTC, datetime
from typing import Any, Dict, Optional

from .kernel_api import KernelAPI
from .models import Task, AgentResult
from .availability import ModelAvailability
from .antigravity_status_module import shared_antigravity_snapshot

logger = logging.getLogger("self_diagnostic")

class SelfDiagnosticModule:
    """
    TDD-implemented module for automatic system-wide diagnostics.
    Verifies modules, memory, and AI provider health.
    """
    name: str = "self_diagnostic"

    def __init__(self):
        self._api: Optional[KernelAPI] = None
        self._is_active: bool = False
        self._availability = ModelAvailability()

    async def on_load(self, api: KernelAPI) -> None:
        self._api = api
        self._is_active = True
        self._api.log("info", f"[DIAG] {self.name} module initialized.")

    async def on_unload(self) -> None:
        self._is_active = False

    def before_task(self, task: Task, context: Dict[str, Any]) -> None:
        """Hook: No-op for diagnostic module."""
        pass

    def after_task(self, task: Any, result: Any, context: Dict[str, Any]) -> None:
        """Hook: No-op for diagnostic module."""
        pass

    async def run_diagnostics(self) -> Dict[str, Any]:
        """
        Executes a full diagnostic scan of the kernel and its environment.
        """
        report = {
            "status": "healthy",
            "timestamp": datetime.now(UTC).isoformat(),
            "components": {},
            "memory": {},
            "ai_models": {}
        }

        # 1. Component (Module) Discovery & Health
        if self._api:
            module_state = self._api.module_state() if hasattr(self._api, "module_state") else {}
            module_manager = self._api.get_context("module_manager")
            if module_manager:
                for mod_name in module_manager.loaded_modules():
                    if mod_name == self.name:
                        continue
                    try:
                        mod = module_manager.get_module(mod_name)
                        mod_report = module_state.get(mod_name, {}) if isinstance(module_state, dict) else {}
                        if not mod_report and hasattr(mod, "finalize"):
                            mod_report = mod.finalize()
                        report["components"][mod_name] = {
                            "status": "ok",
                            "details": mod_report
                        }
                    except Exception as e:
                        report["components"][mod_name] = {
                            "status": "error",
                            "error": str(e)
                        }
                        report["status"] = "degraded"

        # 2. Memory Health
        if self._api:
            memory = self._api.get_memory()
            if memory:
                try:
                    backend_type = type(memory.backend).__name__ if hasattr(memory, "backend") else "unknown"
                    report["memory"] = {
                        "status": "ok",
                        "backend": backend_type,
                        "session_count": len(memory._sessions) if hasattr(memory, "_sessions") else 0
                    }
                except Exception as e:
                    report["memory"] = {"status": "error", "error": str(e)}
                    report["status"] = "degraded"
            else:
                report["memory"] = {"status": "missing"}
                report["status"] = "degraded"

        # 3. AI Model Health (External & Local)
        try:
            # ModelAvailability.check_all() is sync but probes network/processes
            provider_health = self._availability.check_all()
            report["ai_models"] = {p: h.as_dict() for p, h in provider_health.items()}
            report["antigravity_status"] = shared_antigravity_snapshot(force=False)
            
            # Check for local LLM (Ollama) specifically if not in provider_health
            local_model = os.getenv("AI_BRIDGE_LOCAL_LLM_MODEL")
            if local_model and "local" not in report["ai_models"]:
                 report["ai_models"]["local"] = {
                     "model": local_model,
                     "status": "checked_via_env"
                 }

            if any(h.get("status") != "healthy" for h in report["ai_models"].values() if isinstance(h, dict) and "status" in h):
                if report["status"] == "healthy":
                    report["status"] = "degraded"
        except Exception as e:
            report["ai_models"] = {"status": "error", "error": str(e)}
            report["status"] = "degraded"

        return report

    def finalize(self) -> Dict[str, Any]:
        """Module summary for reporting."""
        return {
            "status": "active" if self._is_active else "inactive",
            "capabilities": ["self_diagnostic", "system_health"]
        }
