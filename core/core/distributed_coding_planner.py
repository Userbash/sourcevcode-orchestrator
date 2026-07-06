from __future__ import annotations

from collections import defaultdict
from typing import Any

from .distributed_coding_protocol import DistributedCodingTask, JSONThemes
from .frame_orchestrator import FrameWorkerRole
from .message_bus import MessageBus
from .models import Task


class DistributedCodingPlanner:
    _ROLE_CAPABILITIES: dict[str, tuple[str, ...]] = {
        "core_logic": ("code", "fix", "refactor"),
        "database_storage": ("code", "fix"),
        "validation_security": ("review", "code", "fix"),
        "qa_test_automation": ("test", "code"),
    }

    def _code_agents(self, bus: MessageBus, dispatch_agent_id: str) -> list[str]:
        discovered: list[str] = []
        for capability in ("code", "fix", "refactor"):
            for agent_id in bus.discover_peers(capability):
                if agent_id == dispatch_agent_id or agent_id in discovered:
                    continue
                discovered.append(agent_id)
        return discovered

    def _agents_for_capabilities(
        self,
        bus: MessageBus,
        dispatch_agent_id: str,
        capabilities: tuple[str, ...],
        *,
        exclude: set[str] | None = None,
    ) -> list[str]:
        excluded = exclude or set()
        discovered: list[str] = []
        for capability in capabilities:
            for agent_id in bus.discover_peers(capability):
                if agent_id == dispatch_agent_id or agent_id in excluded or agent_id in discovered:
                    continue
                discovered.append(agent_id)
        return discovered

    @staticmethod
    def _requested_parallelism(task: Task) -> int:
        hints = task.routing_hints if isinstance(task.routing_hints, dict) else {}
        try:
            return max(1, int(hints.get("parallel_branches") or 10))
        except (TypeError, ValueError):
            return 10

    @staticmethod
    def _group_key(file_path: str) -> str:
        normalized = str(file_path or "").strip().replace('\\', '/')
        if not normalized:
            return 'misc'
        parts = [part for part in normalized.split('/') if part]
        if len(parts) >= 2:
            return '/'.join(parts[:2])
        return parts[0]

    def _group_files_by_area(self, files: list[str], max_groups: int) -> list[list[str]]:
        if not files:
            return [[]]
        grouped: dict[str, list[str]] = defaultdict(list)
        for file_path in files:
            grouped[self._group_key(file_path)].append(file_path)
        ordered_groups = sorted(grouped.values(), key=lambda row: (-len(row), row[0]))
        if len(ordered_groups) <= max_groups:
            return ordered_groups
        merged: list[list[str]] = [[] for _ in range(max_groups)]
        for index, group in enumerate(ordered_groups):
            merged[index % max_groups].extend(group)
        return [sorted(group) for group in merged if group]

    @staticmethod
    def _lane_kind(index: int, total: int) -> str:
        if total <= 1:
            return 'implement'
        if index == 0:
            return 'primary'
        if index == total - 1:
            return 'integration'
        return 'secondary'

    @staticmethod
    def _focus_prompt(file_targets: list[str], lane_kind: str) -> str:
        if file_targets:
            areas = sorted({target.split('/')[0] for target in file_targets if '/' in target} or {file_targets[0]})
            return f"Own {lane_kind} lane for: {', '.join(areas)}. Avoid touching files outside the shard unless strictly required by compilation boundaries."
        return f"Own the {lane_kind} lane. Minimize overlap with sibling agents and document assumptions explicitly."

    @staticmethod
    def _frame_roles(task: Task) -> list[FrameWorkerRole]:
        hints = task.routing_hints if isinstance(task.routing_hints, dict) else {}
        package = hints.get("frame_orchestrator")
        if not isinstance(package, dict):
            return []
        validation = package.get("validation")
        if not isinstance(validation, dict):
            return []
        roles = validation.get("worker_roles")
        if not isinstance(roles, list):
            return []
        result: list[FrameWorkerRole] = []
        for item in roles:
            if isinstance(item, dict):
                result.append(FrameWorkerRole(**item))
        return result

    def _frame_orchestrated_task(self, task: Task, bus: MessageBus, *, dispatch_agent_id: str) -> DistributedCodingTask | None:
        roles = self._frame_roles(task)
        if not roles:
            return None
        requested_parallelism = self._requested_parallelism(task)
        selected_agents: list[str] = []
        shard_specs: list[dict[str, Any]] = []
        dropped_roles: list[str] = []
        used_agents: set[str] = set()
        fallback_pool = self._code_agents(bus, dispatch_agent_id)
        for role in roles[:requested_parallelism]:
            capability_order = self._ROLE_CAPABILITIES.get(role.role, (role.target_capability or "code",))
            candidates = self._agents_for_capabilities(bus, dispatch_agent_id, capability_order, exclude=used_agents)
            agent_id = candidates[0] if candidates else next((item for item in fallback_pool if item not in used_agents), None)
            if not agent_id:
                dropped_roles.append(role.role)
                continue
            used_agents.add(agent_id)
            selected_agents.append(agent_id)
            lane_kind = role.role
            shard_specs.append(
                {
                    "target_agent": agent_id,
                    "target_capability": role.target_capability or capability_order[0],
                    "worker_role": role.role,
                    "file_targets": list(role.file_targets),
                    "objective": role.objective or task.input.description,
                    "acceptance_criteria": list(role.acceptance_criteria or task.input.acceptance_criteria),
                    "dependencies": list(role.dependencies),
                    "lane_kind": lane_kind,
                    "focus_prompt": role.focus_prompt or self._focus_prompt(list(role.file_targets), lane_kind),
                    "json_themes": {
                        "primary": ["code", task.type.value, lane_kind],
                        "secondary": ["frame_orchestrated", "isolated_vfs"],
                        "tags": ["distributed", "frame_package", role.role],
                    },
                }
            )
        if not shard_specs:
            return None
        return DistributedCodingTask(
            workflow_id=task.task_id,
            objective=task.input.description,
            repo_path=task.context.repo_path or ".",
            branch=task.context.branch or "main",
            target_agents=selected_agents,
            file_targets=list(task.input.files),
            acceptance_criteria=list(task.input.acceptance_criteria),
            json_themes=JSONThemes(
                primary=["code", task.type.value, "frame_orchestrated"],
                secondary=["asyncio", "rabbitmq", "xml_package"],
                tags=["distributed", "frame_package", "parallel"],
            ),
            max_parallelism=max(1, len(selected_agents)),
            metadata={
                "parent_task_id": task.parent_task_id,
                "session_id": task.session_id,
                "planner": "distributed_coding_frame_v1",
                "requested_parallelism": requested_parallelism,
                "effective_parallelism": len(selected_agents),
                "dropped_roles": dropped_roles,
            },
            shard_specs=shard_specs,
        )

    def build_task(self, task: Task, bus: MessageBus, *, dispatch_agent_id: str) -> DistributedCodingTask:
        frame_task = self._frame_orchestrated_task(task, bus, dispatch_agent_id=dispatch_agent_id)
        if frame_task is not None:
            return frame_task
        candidates = self._code_agents(bus, dispatch_agent_id)
        requested_parallelism = self._requested_parallelism(task)
        file_targets = list(task.input.files)
        safe_parallelism = requested_parallelism if file_targets else 1
        selected_agents = candidates[: max(1, min(len(candidates), safe_parallelism))]
        groups = self._group_files_by_area(file_targets, max(1, len(selected_agents) or 1))
        shard_specs: list[dict[str, Any]] = []
        for index, agent_id in enumerate(selected_agents[: len(groups)]):
            shard_files = list(groups[index]) if index < len(groups) else []
            lane_kind = self._lane_kind(index, len(selected_agents[: len(groups)]))
            shard_specs.append(
                {
                    'target_agent': agent_id,
                    'file_targets': shard_files,
                    'objective': task.input.description,
                    'acceptance_criteria': list(task.input.acceptance_criteria),
                    'lane_kind': lane_kind,
                    'focus_prompt': self._focus_prompt(shard_files, lane_kind),
                    'json_themes': {
                        'primary': ['code', task.type.value, lane_kind],
                        'secondary': ['parallel', 'isolated_vfs'],
                        'tags': ['distributed', 'planner_v2'],
                    },
                }
            )
        themes = JSONThemes(
            primary=['code', task.type.value],
            secondary=['asyncio', 'rabbitmq', 'tdd'],
            tags=['distributed', 'isolated_vfs', 'parallel'],
        )
        return DistributedCodingTask(
            workflow_id=task.task_id,
            objective=task.input.description,
            repo_path=task.context.repo_path or '.',
            branch=task.context.branch or 'main',
            target_agents=selected_agents,
            file_targets=file_targets,
            acceptance_criteria=list(task.input.acceptance_criteria),
            json_themes=themes,
            max_parallelism=max(1, len(selected_agents)),
            metadata={
                'parent_task_id': task.parent_task_id,
                'session_id': task.session_id,
                'planner': 'distributed_coding_v2',
                'requested_parallelism': requested_parallelism,
                'effective_parallelism': max(1, len(selected_agents)),
            },
            shard_specs=shard_specs,
        )
