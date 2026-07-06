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

from core.core.dependency_manager import DependencyManager
from core.core.env_loader import load_env_file
from core.core.orchestration_config import OrchestrationConfig
from core.core.orchestrator import Orchestrator
from core.core.provider_credentials import has_usable_credential
from core.core.security import SecurityManager, SecurityPolicy


def _load_runtime_env() -> None:
    load_env_file()
    load_env_file(".env.bridge", override=True)
    load_env_file(".env.gemini.local", override=True)
    load_env_file("/app/.env.bridge")


def _attach_placeholder_agents() -> bool:
    return os.getenv("AI_BRIDGE_ATTACH_PLACEHOLDER_AGENTS", "false").strip().lower() in {"1", "true", "yes", "on"}


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


def _http_api_enabled() -> bool:
    raw = os.getenv("AI_BRIDGE_API_ENABLED", "true").strip().lower()
    return raw in {"1", "true", "yes", "on"}


def _start_http_api(orchestrator: Orchestrator, starter=None) -> bool:
    if not _http_api_enabled():
        return False
    if starter is None:
        from core.scripts.orchestrator_daemon import _start_http_server as starter
    starter(orchestrator)
    return True


def _attach_default_agents(orchestrator: Orchestrator) -> None:
    from core.agents.ai_kernel_agent import AIKernelAgent
    from core.agents.antigravity_cli_agent import AntigravityCLIAgent
    from core.agents.codex_agent import CodexAgent
    from core.agents.distributed_coder_agent import DistributedCoderAgent
    from core.agents.result_merger_agent import ResultMergerAgent
    from core.agents.local_llm_agent import LocalLLMAgent
    from core.agents.mimo_agent import MimoAgent
    from core.agents.mistral_agent import MistralAgent
    from core.agents.planner_agent import PlannerAgent
    from core.agents.reviewer_agent import ReviewerAgent
    from core.agents.tester_agent import TesterAgent

    security_manager = SecurityManager(
        SecurityPolicy(allow_shell=True, shell_allowlist=["agy -p", "antigravity -p"])
    )
    codex_model = os.getenv("CODEX_OPENAI_MODEL", "gpt-5.5")
    local_model = os.getenv("AI_BRIDGE_LOCAL_LLM_MODEL", "qwen2.5:32b-instruct-q4_k_m")
    ai_kernel_model = os.getenv("AI_KERNEL_MODEL_ALIAS", "hauhaucs-qwen36-35b-a3b-aggressive:q4_k_m")

    if _attach_placeholder_agents():
        orchestrator.attach_local_agent("planner-1", PlannerAgent("planner-1"), agent_type="planner", critical=True, model_name="gpt-planner", provider="openai")
    orchestrator.attach_local_agent("codex-main", CodexAgent("codex-main"), agent_type="codex", critical=True, model_name=codex_model, provider="openai")
    orchestrator.sync_openai_template_workers(enabled=has_usable_credential("OPENAI_API_KEY"), primary_model=codex_model)
    orchestrator.attach_local_agent("antigravity-cli-1", AntigravityCLIAgent("antigravity-cli-1", security_manager), agent_type="external_ai", critical=False, model_name="antigravity-cli", provider="google")
    orchestrator.attach_local_agent("mistral-1", MistralAgent("mistral-1", security_manager), agent_type="external_ai", critical=False, model_name="mistral-large-latest", provider="mistral")
    if _attach_placeholder_agents():
        orchestrator.attach_local_agent("tester-1", TesterAgent("tester-1"), agent_type="tester", model_name="gpt-test-standard", provider="openai")
        orchestrator.attach_local_agent("reviewer-1", ReviewerAgent("reviewer-1"), agent_type="reviewer", model_name="gpt-review-large", provider="openai")
    orchestrator.attach_local_agent("distributed-coder-1", DistributedCoderAgent(), agent_type="custom", critical=False, model_name="distributed-coder-core", provider="local")
    orchestrator.attach_local_agent("result-merger", ResultMergerAgent(), agent_type="custom", critical=False, model_name="result-merger-core", provider="local")
    orchestrator.attach_local_agent("local-llm-1", LocalLLMAgent("local-llm-1", local_model), agent_type="custom", critical=False, model_name=local_model, provider="local")
    orchestrator.attach_local_agent("ai-kernel-qwen36-1", AIKernelAgent("ai-kernel-qwen36-1"), agent_type="custom", critical=False, model_name=ai_kernel_model, provider="ai_kernel")

    mimo_default_model = os.getenv("AI_BRIDGE_MIMO_DEFAULT_MODEL", "xiaomi/mimo-v2.5-pro")
    if os.getenv("AI_BRIDGE_MIMO_ENABLED", "true").strip().lower() in {"1", "true", "yes", "on"}:
        orchestrator.attach_local_agent("mimo-router-1", MimoAgent("mimo-router-1", default_model=mimo_default_model), agent_type="external_ai", critical=False, model_name=mimo_default_model, provider="mimo")


async def main(argv: list[str] | None = None) -> None:
    _load_runtime_env()
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
    _attach_default_agents(orchestrator)
    _start_http_api(orchestrator)

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
