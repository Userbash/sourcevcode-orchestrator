from __future__ import annotations

import asyncio

from core.agents.distributed_coder_agent import DistributedCoderAgent
from core.adapters.state.memory_state_store import MemoryWorkflowStateStore
from core.core.distributed_coding_protocol import (
    DistributedCodingTask,
    JSONThemes,
    WorkShardStatus,
)
from core.core.message_bus import MessageBus
from core.core.models import Priority, Task, TaskContext, TaskInput, TaskStatus, TaskType


class _InspectableBus(MessageBus):
    def __init__(self) -> None:
        super().__init__()
        self.published: list[tuple[str, object]] = []

    def publish(self, topic: str, message: object) -> None:
        self.published.append((topic, message))
        super().publish(topic, message)


def test_distributed_coding_task_schema_keeps_json_themes_and_queue_names():
    async def scenario() -> None:
        payload = DistributedCodingTask(
            objective="Split API/service/repository implementation into isolated code lanes",
            repo_path=".",
            branch="main",
            target_agents=["codex-main", "codex-alt", "codex-third"],
            file_targets=["core/api/router.py", "core/services/coder.py", "core/repos/task_repo.py"],
            acceptance_criteria=["tests pass", "no merge conflicts"],
            json_themes=JSONThemes(primary=["api", "service"], secondary=["repository"], tags=["tdd", "async"]),
            max_parallelism=3,
            shard_specs=[
                {"target_agent": "codex-main", "file_targets": ["core/api/router.py"], "lane_kind": "primary"},
                {"target_agent": "codex-alt", "file_targets": ["core/services/coder.py"], "lane_kind": "secondary"},
                {"target_agent": "codex-third", "file_targets": ["core/repos/task_repo.py"], "lane_kind": "integration"},
            ],
        )

        shards = payload.build_shards()

        assert [shard.queue_name for shard in shards] == [
            "agent.codex-main.tasks",
            "agent.codex-alt.tasks",
            "agent.codex-third.tasks",
        ]
        assert shards[0].lane_kind == "primary"
        assert shards[0].json_themes.primary == ["api", "service"]
        assert all(shard.status == WorkShardStatus.PENDING for shard in shards)

    asyncio.run(scenario())


def test_distributed_coder_agent_dispatches_code_only_shards_and_persists_workflow_events():
    async def scenario() -> None:
        bus = _InspectableBus()
        bus.register_pod("codex-main", ["code"])
        bus.register_pod("codex-alt", ["code", "fix"])
        bus.register_pod("tester-1", ["test"])
        bus.register_pod("reviewer-1", ["review"])
        store = MemoryWorkflowStateStore()
        agent = DistributedCoderAgent(message_bus=bus, state_store=store)

        task = Task(
            TaskType.CODE,
            TaskInput(
                "Implement async distributed coding flow",
                files=[
                    "core/api/router.py",
                    "core/api/controller.py",
                    "core/services/coder.py",
                    "core/repos/task_repo.py",
                ],
                acceptance_criteria=["tests pass", "persist task transitions"],
            ),
            TaskContext("demo", ".", "main"),
            Priority.HIGH,
        )
        task.routing_hints = {"parallel_branches": 3}

        result = await agent.run_async(task)

        assert result.status == TaskStatus.DONE
        assert "Dispatched 2 shards" in result.output.summary
        workflow = store.get_workflow(task.task_id)
        assert workflow is not None
        assert workflow["status"] == "dispatched"
        assert workflow["shard_count"] == 2
        assert workflow["planner"] == "distributed_coding_v2"

        events = store.events[task.task_id]
        assert [event["event_type"] for event in events] == [
            "distributed_coding.received",
            "distributed_coding.shard_dispatched",
            "distributed_coding.shard_dispatched",
            "distributed_coding.completed",
        ]

        task_messages = [message for topic, message in bus.published if topic.endswith(".tasks")]
        task_topics = [topic for topic, _ in bus.published if topic.endswith(".tasks")]
        assert task_topics == [
            "agent.codex-main.tasks",
            "agent.codex-alt.tasks",
        ]
        assert all(payload.target_agent in {"codex-main", "codex-alt"} for payload in task_messages)
        assert all("LANE FOCUS:" in payload.payload.objective for payload in task_messages)
        assert any(topic == "orchestrator.results" for topic, _ in bus.published)

    asyncio.run(scenario())


def test_distributed_coder_agent_can_fan_out_to_ten_code_agents():
    async def scenario() -> None:
        bus = _InspectableBus()
        for idx in range(10):
            bus.register_pod(f"codex-{idx + 1}", ["code"])
        store = MemoryWorkflowStateStore()
        agent = DistributedCoderAgent(message_bus=bus, state_store=store)

        task = Task(
            TaskType.CODE,
            TaskInput(
                "Implement ten isolated code lanes in parallel",
                files=[f"services/service_{idx + 1}.py" for idx in range(10)],
                acceptance_criteria=["tests pass", "fanout across ten agents"],
            ),
            TaskContext("demo", ".", "main"),
            Priority.HIGH,
        )
        task.routing_hints = {"parallel_branches": 10}

        result = await agent.run_async(task)

        assert result.status == TaskStatus.DONE
        assert "Dispatched 10 shards" in result.output.summary
        task_topics = [topic for topic, _ in bus.published if topic.endswith(".tasks")]
        assert len(task_topics) == 10
        assert len(set(task_topics)) == 10

    asyncio.run(scenario())


def test_distributed_coder_agent_limits_parallelism_without_file_boundaries():
    async def scenario() -> None:
        bus = _InspectableBus()
        bus.register_pod("codex-main", ["code"])
        bus.register_pod("codex-alt", ["code"])
        store = MemoryWorkflowStateStore()
        agent = DistributedCoderAgent(message_bus=bus, state_store=store)
        task = Task(
            TaskType.CODE,
            TaskInput("Implement async coding flow without explicit file map", acceptance_criteria=["tests pass"]),
            TaskContext("demo", ".", "main"),
        )
        task.routing_hints = {"parallel_branches": 3}

        result = await agent.run_async(task)

        assert result.status == TaskStatus.DONE
        assert "Dispatched 1 shards" in result.output.summary
        task_topics = [topic for topic, _ in bus.published if topic.endswith(".tasks")]
        assert task_topics == ["agent.codex-main.tasks"]

    asyncio.run(scenario())


def test_distributed_coder_agent_fails_when_no_parallel_peers_are_available():
    async def scenario() -> None:
        bus = _InspectableBus()
        store = MemoryWorkflowStateStore()
        agent = DistributedCoderAgent(message_bus=bus, state_store=store)
        task = Task(
            TaskType.CODE,
            TaskInput("Implement async coding flow", files=["core/api/router.py"]),
            TaskContext("demo", ".", "main"),
        )

        result = await agent.run_async(task)

        assert result.status == TaskStatus.FAILED
        assert result.errors == ["no_parallel_peers_available"]
        workflow = store.get_workflow(task.task_id)
        assert workflow is not None
        assert workflow["status"] == "failed"
        assert any(topic == "orchestrator.results" for topic, _ in bus.published)

    asyncio.run(scenario())



def test_distributed_coder_agent_uses_frame_roles_for_capability_aware_dispatch():
    async def scenario() -> None:
        bus = _InspectableBus()
        bus.register_pod("codex-main", ["code"])
        bus.register_pod("reviewer-1", ["review"])
        bus.register_pod("tester-1", ["test"])
        store = MemoryWorkflowStateStore()
        agent = DistributedCoderAgent(message_bus=bus, state_store=store)

        task = Task(
            TaskType.CODE,
            TaskInput(
                "Implement websocket ingestion with validation and tests",
                files=[
                    "core/ws/router.py",
                    "core/security/validator.py",
                    "core/test/test_ws_router.py",
                ],
                acceptance_criteria=["tests pass", "validation schemas generated"],
            ),
            TaskContext("demo", ".", "main"),
            Priority.HIGH,
        )
        task.routing_hints = {
            "parallel_branches": 4,
            "frame_orchestrator": {
                "validation": {
                    "worker_roles": [
                        {
                            "role": "core_logic",
                            "target_capability": "code",
                            "file_targets": ["core/ws/router.py"],
                            "objective": task.input.description,
                            "acceptance_criteria": ["tests pass"],
                            "dependencies": [],
                            "focus_prompt": "Own core logic lane.",
                        },
                        {
                            "role": "validation_security",
                            "target_capability": "review",
                            "file_targets": ["core/security/validator.py"],
                            "objective": task.input.description,
                            "acceptance_criteria": ["validation schemas generated"],
                            "dependencies": ["core_logic"],
                            "focus_prompt": "Own validation lane.",
                        },
                        {
                            "role": "qa_test_automation",
                            "target_capability": "test",
                            "file_targets": ["core/test/test_ws_router.py"],
                            "objective": task.input.description,
                            "acceptance_criteria": ["tests pass"],
                            "dependencies": ["core_logic"],
                            "focus_prompt": "Own QA lane.",
                        },
                    ]
                }
            },
        }

        result = await agent.run_async(task)

        assert result.status == TaskStatus.DONE
        assert "Dispatched 3 shards" in result.output.summary
        task_messages = [message for topic, message in bus.published if topic.endswith(".tasks")]
        assert [payload.target_capability for payload in task_messages] == ["code", "review", "test"]
        assert [payload.target_agent for payload in task_messages] == ["codex-main", "reviewer-1", "tester-1"]

    asyncio.run(scenario())
