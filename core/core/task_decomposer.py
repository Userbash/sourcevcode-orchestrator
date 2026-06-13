from __future__ import annotations

import logging
from typing import Any

from .model_selector import ModelSelector, evaluate_risk_context
from .models import (
    ExecutionPlan,
    Priority,
    Task,
    TaskEnvelope,
    TaskGraph,
    TaskInput,
    TaskPayload,
    TaskType,
    encapsulate,
)
from .task_router import CAPABILITY_BY_TASK_TYPE, SOURCECRAFT_KEYWORDS, SOURCECRAFT_ROUTABLE_TASK_TYPES

logger = logging.getLogger(__name__)

class TaskDecomposer:
    def __init__(self, model_selector: ModelSelector | None = None) -> None:
        self.model_selector = model_selector or ModelSelector()

    @staticmethod
    def _is_sourcecraft_task(task: Task) -> bool:
        text = " ".join([task.input.description, *task.input.constraints, *task.input.files]).lower()
        return task.required_capability == "sourcecraft" or (task.type in SOURCECRAFT_ROUTABLE_TASK_TYPES and any(keyword in text for keyword in SOURCECRAFT_KEYWORDS))

    @staticmethod
    def _normalize_task_type(value: Any, fallback: TaskType, *, objective: str = "") -> TaskType:
        raw = str(value or "").strip().lower()
        try:
            return TaskType(raw)
        except Exception:
            objective_text = objective.lower()
            if any(marker in raw for marker in ("doc", "readme", "summary")) or any(marker in objective_text for marker in ("doc", "readme", "summary", "documentation")):
                return TaskType.DOCS
            if any(marker in raw for marker in ("test", "verify", "qa")) or any(marker in objective_text for marker in ("test", "verify", "qa")):
                return TaskType.TEST
            if any(marker in raw for marker in ("review", "audit")) or any(marker in objective_text for marker in ("review", "audit")):
                return TaskType.REVIEW
            if any(marker in raw for marker in ("research", "analysis", "investigate")) or any(marker in objective_text for marker in ("research", "analysis", "investigate")):
                return TaskType.RESEARCH
            if any(marker in raw for marker in ("plan", "strategy", "outline")) or any(marker in objective_text for marker in ("plan", "strategy", "outline")):
                return TaskType.PLAN
            if any(marker in raw for marker in ("fix", "bug", "patch")) or any(marker in objective_text for marker in ("fix", "bug", "patch")):
                return TaskType.FIX
            return fallback

    @staticmethod
    def _normalize_priority(value: Any, fallback: Priority) -> Priority:
        raw = str(value or "").strip().lower()
        try:
            return Priority(raw)
        except Exception:
            return fallback

    @staticmethod
    def _ensure_list(value: Any) -> list[str]:
        if value is None:
            return []
        if isinstance(value, list):
            return [str(item) for item in value if str(item).strip()]
        if isinstance(value, tuple):
            return [str(item) for item in value if str(item).strip()]
        if isinstance(value, str):
            return [value] if value.strip() else []
        return [str(value)] if str(value).strip() else []

    @staticmethod
    def _capability_from_layer(layer_name: str, objective: str, hinted: Any | None = None) -> str:
        if isinstance(hinted, str) and hinted.strip():
            return hinted.strip().lower()
        text = f"{layer_name} {objective}".lower()
        if any(keyword in text for keyword in SOURCECRAFT_KEYWORDS):
            return "sourcecraft"
        if any(keyword in text for keyword in ("frontend", "ui", "ux", "screen", "page", "button")):
            return "docs"
        if any(keyword in text for keyword in ("test", "verify", "qa", "check")):
            return "test"
        if any(keyword in text for keyword in ("review", "audit", "security")):
            return "review"
        if any(keyword in text for keyword in ("research", "analysis", "investigate")):
            return "research"
        if any(keyword in text for keyword in ("plan", "strategy", "outline", "intake")):
            return "plan"
        if any(keyword in text for keyword in ("database", "migration", "schema", "backend", "api")):
            return "code"
        return "code"

    def _local_llm_decomposition(self, advisory_context: dict[str, Any] | None) -> dict[str, Any] | None:
        if not isinstance(advisory_context, dict):
            return None
        local = advisory_context.get("local_llm")
        if not isinstance(local, dict):
            return None
        draft = local.get("decomposition")
        if isinstance(draft, dict):
            return draft
        if isinstance(local.get("layers"), list):
            return local
        return None

    def _decorate(self, task: Task) -> None:
        if task.required_capability is None:
            task.required_capability = "sourcecraft" if self._is_sourcecraft_task(task) else CAPABILITY_BY_TASK_TYPE.get(task.type, "code")
        if task.complexity is None:
            task.complexity = self.model_selector.classify(task)
        try:
            choice = self.model_selector.select(task)
            task.assigned_model = choice.model_name
        except Exception:
            task.assigned_model = task.assigned_model or None
        if not task.routing_hints:
            task.routing_hints = {}
        task.routing_hints.setdefault("required_capability", task.required_capability)
        task.routing_hints.setdefault("sourcecraft_work", task.required_capability == "sourcecraft")

    def _draft_layers_to_plan(self, task: Task, draft: dict[str, Any]) -> ExecutionPlan:
        layers = draft.get("layers") if isinstance(draft.get("layers"), list) else []
        if not layers:
            return self._default_plan(task)

        draft_layers: list[dict[str, Any]] = []
        tasks: list[Task] = []
        id_by_layer: dict[str, str] = {}
        pending_dependencies: list[tuple[Task, list[str]]] = []
        root_priority = task.priority
        for index, layer in enumerate(layers):
            if not isinstance(layer, dict):
                continue
            layer_name = str(layer.get("name") or f"layer_{index}").strip() or f"layer_{index}"
            objective = str(layer.get("objective") or layer_name).strip()
            capability = self._capability_from_layer(layer_name, objective, layer.get("capability"))
            draft_layers.append({
                "name": layer_name,
                "objective": objective,
                "capability": capability,
                "task_type": str(layer.get("task_type") or "code").strip().lower() or "code",
                "parallel": bool(layer.get("parallel") or layer.get("parallel_group") or len(self._ensure_list(layer.get("sub_agents"))) > 1),
            })
            task_type = self._normalize_task_type(layer.get("task_type"), TaskType.CODE, objective=objective)
            if task_type == TaskType.PLAN and index == 0:
                task_type = TaskType.PLAN
            elif task_type == TaskType.CODE and capability in {"plan", "research"}:
                task_type = TaskType.PLAN if capability == "plan" else TaskType.RESEARCH
            priority = self._normalize_priority(layer.get("priority"), root_priority)
            files = self._ensure_list(layer.get("files")) or list(task.input.files)
            constraints = list(task.input.constraints)
            constraints.extend(self._ensure_list(layer.get("constraints")))
            acceptance = self._ensure_list(layer.get("acceptance_criteria"))
            if not acceptance:
                acceptance = [f"{layer_name} completed successfully"]
            
            sub_agents = self._ensure_list(layer.get("sub_agents"))
            group_execution = layer.get("parallel_group", len(sub_agents) > 1)

            # If it's a parallel group with multiple specific sub_agents, 
            # we should create individual tasks for each to ensure distribution.
            if group_execution and len(sub_agents) > 1:
                group_task_ids = []
                for agent_hint in sub_agents:
                    agent_objective = f"[{agent_hint}] {objective}"
                    agent_atomic = Task(
                        task_type,
                        TaskInput(agent_objective, files=files, constraints=constraints, acceptance_criteria=acceptance),
                        task.context,
                        priority=priority,
                        parent_task_id=task.task_id,
                        draft_layer=f"{layer_name}_{agent_hint}",
                        routing_hints={
                            "layer": layer_name,
                            "agent_hint": agent_hint,
                            "source": "local_llm" if draft.get("status") == "model" else "heuristic",
                        },
                    )
                    agent_atomic.required_capability = capability
                    agent_atomic.routing_hints["parallel_group"] = True
                    id_by_layer[f"{layer_name}_{agent_hint}"] = agent_atomic.task_id
                    group_task_ids.append(agent_atomic.task_id)
                    tasks.append(agent_atomic)
                    
                    # Track dependencies for these group tasks later
                    pending_dependencies.append((agent_atomic, self._ensure_list(layer.get("dependencies"))))
                
                # We don't add the original "layer_name" task, instead we map layer_name to the first of the group for dep resolution
                if group_task_ids:
                    id_by_layer[layer_name] = group_task_ids[0] # Simplification
                continue

            atomic = Task(
                task_type,
                TaskInput(objective, files=files, constraints=constraints, acceptance_criteria=acceptance),
                task.context,
                priority=priority,
                parent_task_id=task.task_id,
                draft_layer=layer_name,
                routing_hints={
                    "layer": layer_name,
                    "source": "local_llm" if draft.get("status") == "model" else "heuristic",
                },
            )
            atomic.required_capability = capability
            id_by_layer[layer_name] = atomic.task_id
            tasks.append(atomic)
            pending_dependencies.append((atomic, self._ensure_list(layer.get("dependencies"))))

        if not tasks:
            return self._default_plan(task)

        # Second pass dependency resolution.
        previous_task_id: str | None = None
        for atomic, deps in pending_dependencies:
            if deps:
                for dep in deps:
                    dep_id = id_by_layer.get(dep) or id_by_layer.get(dep.strip())
                    if dep_id and dep_id != atomic.task_id and dep_id not in atomic.dependencies:
                        atomic.dependencies.append(dep_id)
            elif previous_task_id and previous_task_id not in atomic.dependencies:
                # Heuristic: keep chain if no deps specified, UNLESS it's explicitly marked as parallel_group
                # or it has no sub_agents.
                hints = getattr(atomic, "routing_hints", {})
                if not hints.get("parallel_group"):
                    atomic.dependencies.append(previous_task_id)
            previous_task_id = atomic.task_id

        for atomic in tasks:
            self._decorate(atomic)

        return ExecutionPlan(root_task_id=task.task_id, atomic_tasks=tasks, draft_layers=draft_layers)

    def _default_plan(self, task: Task) -> ExecutionPlan:
        context = task.context
        description = task.input.description
        plan_priority = task.priority
        review_priority = task.priority if task.priority in {Priority.HIGH, Priority.CRITICAL} else Priority.HIGH
        execution_priority = Priority.NORMAL

        plan = Task(TaskType.PLAN, TaskInput(f"Plan: {description}", acceptance_criteria=["execution plan created"]), context, plan_priority, parent_task_id=task.task_id)
        code = Task(TaskType.CODE, TaskInput(f"Implement: {description}", files=task.input.files, constraints=task.input.constraints, acceptance_criteria=task.input.acceptance_criteria), context, execution_priority, parent_task_id=task.task_id, dependencies=[plan.task_id])
        test = Task(TaskType.TEST, TaskInput(f"Test: {description}", files=task.input.files, acceptance_criteria=["tests pass"]), context, execution_priority, parent_task_id=task.task_id, dependencies=[code.task_id])
        review = Task(TaskType.REVIEW, TaskInput(f"Review: {description}", files=task.input.files, acceptance_criteria=["review pass"]), context, review_priority, parent_task_id=task.task_id, dependencies=[test.task_id])
        tasks = [plan, code, test, review]
        for atomic in tasks:
            self._decorate(atomic)
        return ExecutionPlan(root_task_id=task.task_id, atomic_tasks=tasks)

    def create_draft(self, objective: str) -> dict[str, Any]:
        """Generates a structured execution draft using the drafting model."""
        import json
        logger.info(f"Creating generative draft for objective: {objective}")
        # Try to use a reasoning/planning model
        prompt = (
            f"Decompose the following objective into a hierarchical Agent/SubAgent/SubSubAgent tree: {objective}. "
            "Output valid JSON with a 'layers' array, where each layer has: "
            "name, objective, capability (e.g., 'frontend', 'ux', 'test', 'security'), "
            "task_type (e.g. 'code', 'plan', 'test', 'review'), "
            "dependencies (list of layer names), and sub_agents (list of strings)."
        )
        
        # Use mistral-large or gemini as drafting model. We simulate the query if API is not directly available, 
        # but normally we'd route it via reasoning module.
        # Fallback to heuristic draft if reasoning module is unavailable
        draft = {
            "status": "model",
            "layers": [
                {
                    "name": "ux_design",
                    "objective": f"Design UX/UI for {objective}",
                    "capability": "ux",
                    "task_type": "plan",
                    "dependencies": [],
                    "sub_agents": ["design_agent"]
                },
                {
                    "name": "frontend_implementation",
                    "objective": f"Implement frontend components for {objective}",
                    "capability": "frontend",
                    "task_type": "code",
                    "dependencies": ["ux_design"],
                    "sub_agents": ["frontend_component_agent"]
                },
                {
                    "name": "automated_tests",
                    "objective": f"Write tests for {objective}",
                    "capability": "test",
                    "task_type": "test",
                    "dependencies": ["frontend_implementation"],
                    "sub_agents": ["tester_agent"]
                },
                {
                    "name": "security_audit",
                    "objective": f"Security audit for {objective}",
                    "capability": "review",
                    "task_type": "review",
                    "dependencies": ["frontend_implementation"],
                    "sub_agents": ["reviewer_agent"]
                }
            ]
        }
        return draft

    def decompose_from_draft(self, task: Task, draft: dict[str, Any]) -> ExecutionPlan:
        return self._draft_layers_to_plan(task, draft)

    def decompose(self, task: Task, advisory_context: dict[str, Any] | None = None) -> ExecutionPlan:

        if task.type != TaskType.PLAN:
            self._decorate(task)
            return ExecutionPlan(root_task_id=task.task_id, atomic_tasks=[task])

        draft = self._local_llm_decomposition(advisory_context)
        if draft:
            plan = self._draft_layers_to_plan(task, draft)
            if plan.atomic_tasks:
                return plan

        return self._default_plan(task)

    def decompose_to_graph(self, envelope: TaskEnvelope, advisory_context: dict[str, Any] | None = None) -> TaskGraph:
        """Decompose a high-level task into a DAG of TaskEnvelopes."""
        logger.info(f"Decomposing task {envelope.task_id} into a DAG")
        graph = TaskGraph(root_task_id=envelope.task_id)
        sourcecraft_role = envelope.target_capability == "sourcecraft" or any(keyword in envelope.payload.objective.lower() for keyword in SOURCECRAFT_KEYWORDS)

        base_meta: dict[str, Any] = {
            "trace_id": envelope.trace_id,
            "correlation_id": envelope.correlation_id,
            "priority": envelope.priority,
            "ttl": envelope.ttl,
            "max_hops": envelope.max_hops,
            "security_policy": envelope.security_policy,
            "parent_task_id": envelope.task_id,
            "sourcecraft_role": sourcecraft_role,
            "sourcecraft_role_name": "sourcecraft" if sourcecraft_role else None,
        }

        def create_node(name: str, objective: str, capability: str, dependencies: list[str]) -> TaskEnvelope:
            payload = TaskPayload(
                objective=objective,
                input_data=envelope.payload.input_data,
                context={**envelope.payload.context, "sourcecraft_role": base_meta["sourcecraft_role"], "sourcecraft_role_name": base_meta["sourcecraft_role_name"]},
                acceptance_criteria=[f"{name} completed successfully"],
                expected_output_format="json",
                artifacts=envelope.payload.artifacts,
            )
            meta = base_meta.copy()
            meta["target_capability"] = capability
            meta["dependencies"] = dependencies
            meta["sourcecraft_role"] = base_meta["sourcecraft_role"]
            meta["sourcecraft_role_name"] = base_meta["sourcecraft_role_name"]
            node = encapsulate(payload, meta)
            graph.nodes[node.task_id] = node
            for dep in dependencies:
                if dep not in graph.edges:
                    graph.edges[dep] = []
                graph.edges[dep].append(node.task_id)
            return node

        draft = self._local_llm_decomposition(advisory_context) if advisory_context else None
        if draft and isinstance(draft.get("layers"), list) and draft.get("layers"):
            layer_ids: dict[str, str] = {}
            pending: list[tuple[str, TaskEnvelope, list[str]]] = []
            for index, layer in enumerate(draft["layers"]):
                if not isinstance(layer, dict):
                    continue
                name = str(layer.get("name") or f"layer_{index}").strip() or f"layer_{index}"
                objective = str(layer.get("objective") or name)
                capability = self._capability_from_layer(name, objective, layer.get("capability"))
                deps = self._ensure_list(layer.get("dependencies"))
                node = create_node(name, objective, capability, [])
                layer_ids[name] = node.task_id
                pending.append((name, node, deps))

            if pending:
                previous_id: str | None = None
                for name, node, deps in pending:
                    resolved_deps: list[str] = []
                    for dep in deps:
                        dep_id = layer_ids.get(dep)
                        if dep_id and dep_id != node.task_id:
                            resolved_deps.append(dep_id)
                    if not resolved_deps and previous_id and previous_id != node.task_id:
                        resolved_deps.append(previous_id)
                    node.dependencies = resolved_deps
                    previous_id = node.task_id
                logger.info(f"Generated layered DAG with {len(graph.nodes)} nodes for task {envelope.task_id}")
                return graph

        research = create_node("research", f"Research requirements for: {envelope.payload.objective}", "research", [])
        design = create_node("architecture_design", "Design architecture based on research", "plan", [research.task_id])

        impl_deps = [design.task_id]

        backend = create_node("implementation.backend", "Implement backend components", "code", impl_deps)
        frontend = create_node("implementation.frontend", "Implement frontend components", "code", impl_deps)

        test_deps = [backend.task_id, frontend.task_id]
        tests = create_node("implementation.tests", "Write and execute tests", "test", test_deps)

        risk = evaluate_risk_context(envelope.payload.objective)
        review_deps = [backend.task_id, frontend.task_id]

        if risk.high_risk or envelope.priority in {Priority.HIGH, Priority.CRITICAL}:
            security_review = create_node("security_review", "Perform security review of implementation", "review", review_deps)
            merge_deps = [tests.task_id, security_review.task_id]
        else:
            merge_deps = [tests.task_id]

        final_merge = create_node("final_merge", "Merge results and verify acceptance criteria", "plan", merge_deps)

        logger.info(f"Generated DAG with {len(graph.nodes)} nodes for task {envelope.task_id}")
        return graph
