#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.core.kpi_event_logger import KPIEventLogger
from core.core.orchestration_config import OrchestrationConfig


def _resolve_logger_path() -> dict[str, object]:
    configured = (os.getenv("AI_BRIDGE_KPI_LOG_FILE") or "").strip()
    memory_store_dir = (os.getenv("AI_BRIDGE_MEMORY_STORE_DIR") or "memory_store").strip()
    logger = KPIEventLogger.from_env()
    return {
        "configured_env_path": configured or None,
        "memory_store_dir": memory_store_dir,
        "logger_path": str(logger.file_path),
        "summary_path": str(logger.summary_path) if logger.summary_path else None,
        "logger_parent_exists": logger.file_path.parent.exists(),
        "logger_parent_writable": os.access(logger.file_path.parent, os.W_OK) if logger.file_path.parent.exists() else False,
    }


def _resolve_orchestrator_mode() -> dict[str, object]:
    cfg = OrchestrationConfig.from_env()
    logger = KPIEventLogger.from_env()
    return {
        "enabled_by_default": cfg.enabled_by_default,
        "default_mode": cfg.default_mode,
        "default_engine": cfg.default_engine,
        "non_interactive": cfg.non_interactive,
        "ask_confirmation": cfg.ask_confirmation,
        "auto_approve_safe_tasks": cfg.auto_approve_safe_tasks,
        "auto_route_tasks": cfg.auto_route_tasks,
        "auto_start_agents": cfg.auto_start_agents,
        "auto_retry": cfg.auto_retry,
        "auto_review": cfg.auto_review,
        "auto_test": cfg.auto_test,
        "kpi_dashboard_output_path": cfg.kpi_dashboard_output_path,
        "kpi_thresholds_by_task": cfg.kpi_thresholds_by_task,
        "kpi_logger_path": str(logger.file_path),
        "kpi_summary_path": str(logger.summary_path) if logger.summary_path else None,
    }


def main() -> None:
    payload = {
        "paths": _resolve_logger_path(),
        "orchestrator": _resolve_orchestrator_mode(),
    }
    print(json.dumps(payload, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
