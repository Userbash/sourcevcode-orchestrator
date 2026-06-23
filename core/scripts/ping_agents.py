import asyncio
import json
import logging
import os
from core.agents.planner_agent import PlannerAgent
from core.agents.codex_agent import CodexAgent
from core.agents.antigravity_cli_agent import AntigravityCLIAgent
from core.agents.mistral_agent import MistralAgent
from core.agents.reviewer_agent import ReviewerAgent
from core.agents.tester_agent import TesterAgent
from core.core.models import Task, TaskType, Priority, TaskInput, TaskContext
from core.core.security import SecurityManager, SecurityPolicy

logging.basicConfig(level=logging.INFO)
print(f"DEBUG: MISTRAL_API_KEY loaded: {os.getenv('MISTRAL_API_KEY') is not None}")


def _task_type_for_agent(agent) -> TaskType:
    t_type = TaskType.RESEARCH
    if isinstance(agent, PlannerAgent):
        t_type = TaskType.PLAN
    if isinstance(agent, CodexAgent):
        t_type = TaskType.CODE
    if isinstance(agent, ReviewerAgent):
        t_type = TaskType.REVIEW
    if isinstance(agent, TesterAgent):
        t_type = TaskType.TEST
    return t_type


async def _probe_agent(agent):
    task_id = f"ping-{agent.agent_id}"
    task = Task(
        task_id=task_id,
        type=_task_type_for_agent(agent),
        priority=Priority.NORMAL,
        input=TaskInput(description="System ping. Reply with your status and capabilities."),
        context=TaskContext(project="hebrew-web", repo_path=".", branch="main"),
    )
    try:
        result = await asyncio.to_thread(agent.execute, task)
        return agent.agent_id, {
            "status": result.status.value,
            "output": result.output.as_dict(),
            "confidence": result.confidence,
            "provider": getattr(result, "provider", None),
            "model_name": getattr(result, "model_name", None),
            "errors": list(getattr(result, "errors", []) or []),
        }
    except Exception as e:
        return agent.agent_id, {"status": "error", "error": str(e)}


async def main_async():
    security_manager = SecurityManager(SecurityPolicy(allow_shell=True, shell_allowlist=["agy -p", "antigravity -p"]))

    agents = [
        PlannerAgent("planner-1"),
        CodexAgent("codex-main"),
        AntigravityCLIAgent("antigravity-cli-1", security_manager),
        MistralAgent("mistral-1", security_manager),
        TesterAgent("tester-1"),
        ReviewerAgent("reviewer-1")
    ]

    responses = dict(await asyncio.gather(*(_probe_agent(agent) for agent in agents)))
    print(json.dumps(responses, indent=2, ensure_ascii=False))


def main():
    asyncio.run(main_async())


if __name__ == "__main__":
    main()
