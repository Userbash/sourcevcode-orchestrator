from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
if Path("/app").exists():
    sys.path.insert(0, "/app")

from core.agents.codex_agent import CodexAgent
from core.agents.antigravity_cli_agent import AntigravityCLIAgent
from core.agents.mistral_agent import MistralAgent
from core.agents.planner_agent import PlannerAgent
from core.agents.reviewer_agent import ReviewerAgent
from core.agents.tester_agent import TesterAgent
from core.agents.frontend_dev_agent import FrontendDevAgent
from core.agents.frontend_design_agent import FrontendDesignAgent
from core.agents.local_llm_agent import LocalLLMAgent
from core.core.orchestration_config import OrchestrationConfig
from core.core.dependency_manager import DependencyManager
from core.core.orchestrator import Orchestrator
from core.core.security import SecurityManager, SecurityPolicy


def _ensure_memory_dirs() -> None:
    configured_dir = os.getenv("AI_BRIDGE_MEMORY_STORE_DIR", "").strip()
    if configured_dir:
        base = Path(configured_dir)
    else:
        app_dir = Path("/app")
        base = app_dir / "memory_store" if app_dir.exists() and os.access(app_dir, os.W_OK) else Path.cwd() / "memory_store"
    (base / "memories").mkdir(parents=True, exist_ok=True)
    (base / "commands").mkdir(parents=True, exist_ok=True)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run AI Bridge orchestration")
    parser.add_argument("--use-bridge", action="store_true")
    parser.add_argument("--auto", action="store_true")
    parser.add_argument("--yes", action="store_true")
    parser.add_argument("--non-interactive", action="store_true")
    return parser


async def main(argv: list[str] | None = None) -> None:
    _ensure_memory_dirs()
    DependencyManager.ensure_required()
    args = build_parser().parse_args(argv)
    config = OrchestrationConfig.from_env()
    config.apply_cli_flags(
        use_bridge=args.use_bridge,
        auto=args.auto,
        yes=args.yes,
        non_interactive=args.non_interactive,
    )

    missing_optional = DependencyManager.find_missing()["optional"]
    if missing_optional:
        print("Optional AI libs not installed:", ", ".join(missing_optional))

    orchestrator = Orchestrator()
    orchestrator.orchestration_config = config

    security_manager = SecurityManager(SecurityPolicy(allow_shell=True, shell_allowlist=["agy -p", "antigravity -p"]))

    orchestrator.attach_local_agent("planner-1", PlannerAgent("planner-1"), agent_type="planner", critical=True, model_name="gpt-planner", provider="openai")
    orchestrator.attach_local_agent("codex-main", CodexAgent("codex-main"), agent_type="codex", critical=True, model_name=os.getenv("CODEX_OPENAI_MODEL", "gpt-4o"), provider="openai")
    orchestrator.attach_local_agent("antigravity-cli-1", AntigravityCLIAgent("antigravity-cli-1", security_manager), agent_type="external_ai", critical=False, model_name="antigravity-cli", provider="google")
    orchestrator.attach_local_agent("mistral-1", MistralAgent("mistral-1", security_manager), agent_type="external_ai", critical=False, model_name="mistral-large-latest", provider="mistral")
    orchestrator.attach_local_agent("tester-1", TesterAgent("tester-1"), agent_type="tester", model_name="gpt-test-standard", provider="openai")
    orchestrator.attach_local_agent("reviewer-1", ReviewerAgent("reviewer-1"), agent_type="reviewer", model_name="gpt-review-large", provider="openai")
    orchestrator.attach_local_agent("frontend-dev-1", FrontendDevAgent("frontend-dev-1"), agent_type="codex", model_name=os.getenv("CODEX_OPENAI_MODEL", "gpt-4o"), provider="openai")
    orchestrator.attach_local_agent("frontend-design-1", FrontendDesignAgent("frontend-design-1"), agent_type="docs", model_name="design-spec", provider="local")
    orchestrator.attach_local_agent("local-llm-1", LocalLLMAgent("local-llm-1", os.getenv("AI_BRIDGE_LOCAL_LLM_MODEL", "qwen2.5:32b-instruct-q4_k_m")), agent_type="custom", critical=False, model_name=os.getenv("AI_BRIDGE_LOCAL_LLM_MODEL", "qwen2.5:32b-instruct-q4_k_m"), provider="local")

    print(f"System Ready. Agents bound: {len(orchestrator.registry.list_agents())}")
    try:
        await orchestrator.listen_for_tasks()
    except asyncio.CancelledError:
        print("Orchestrator shutdown requested.")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("Orchestrator stopped.")
