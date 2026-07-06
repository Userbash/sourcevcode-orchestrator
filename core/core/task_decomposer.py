from __future__ import annotations

import logging
from typing import Any

from .model_selector import ModelSelector, evaluate_risk_context
from .models import (
    Complexity,
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

    @staticmethod
    def _capability_from_agent_hint(agent_hint: Any, fallback: str) -> str:
        hint = str(agent_hint or "").strip().lower()
        if not hint:
            return fallback
        for marker, capability in (
            ("design", "ux"),
            ("ux", "ux"),
            ("validator", "review"),
            ("review", "review"),
            ("tester", "test"),
            ("test", "test"),
            ("security", "review"),
            ("research", "research"),
            ("planner", "plan"),
            ("plan", "plan"),
            ("code", "code"),
            ("fix", "fix"),
            ("docs", "docs"),
        ):
            if marker in hint:
                return capability
        return fallback

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
        requested_model = str(task.assigned_model or (task.routing_hints or {}).get("requested_model") or "").strip()
        if requested_model:
            task.assigned_model = requested_model
            task.routing_hints.setdefault("requested_model", requested_model)
        else:
            try:
                choice = self.model_selector.select(task)
                task.assigned_model = choice.model_name
            except Exception:
                task.assigned_model = task.assigned_model or None
        if not task.routing_hints:
            task.routing_hints = {}
        task.routing_hints.setdefault("required_capability", task.required_capability)
        task.routing_hints.setdefault("sourcecraft_work", task.required_capability == "sourcecraft")
        task_type = getattr(task.type, "value", task.type)
        kpi_floor = {
            "plan": 0.72,
            "review": 0.76,
            "test": 0.74,
        }.get(str(task_type).lower(), 0.65)
        local_llm = None
        try:
            api = getattr(self.model_selector, "_api", None)
            if api and hasattr(api, "get_module"):
                local_llm = api.get_module("local_llm")
        except Exception:
            local_llm = None
        if local_llm and getattr(local_llm, "ready", False):
            kpi_floor = min(0.95, kpi_floor + 0.05)
            task.routing_hints.setdefault("kpi_floor_source", "local_llm")
        else:
            task.routing_hints.setdefault("kpi_floor_source", "default")
        task.routing_hints.setdefault("kpi_floor", kpi_floor)


    @staticmethod
    def _openai_template_candidates(advisory_context: dict[str, Any] | None, role: str) -> list[dict[str, Any]]:
        if not isinstance(advisory_context, dict):
            return []
        payload = advisory_context.get("openai_compatible")
        if not isinstance(payload, dict):
            return []
        key_map = {
            "code_parallel": "code_parallel_candidates",
            "review_primary": "review_candidates",
            "plan_primary": "plan_candidates",
            "test_primary": "test_candidates",
            "docs_primary": "docs_candidates",
        }
        rows = payload.get(key_map.get(role, role))
        return [row for row in rows if isinstance(row, dict)] if isinstance(rows, list) else []

    def _parallel_code_agents(self) -> list[str]:
        api = getattr(self.model_selector, "_api", None)
        registry = getattr(api, "registry", None) if api is not None else None
        local_agents = getattr(api, "local_agents", {}) if api is not None else {}
        if registry is None or not hasattr(registry, "list_agents"):
            return []
        agents: list[str] = []
        for record in registry.list_agents():
            if record.id not in local_agents:
                continue
            if "code" not in getattr(record, "capabilities", []):
                continue
            agents.append(record.id)
        return agents

    def _parallel_code_plan(self, task: Task, advisory_context: dict[str, Any] | None = None) -> ExecutionPlan | None:
        hints = task.routing_hints if isinstance(task.routing_hints, dict) else {}
        complexity = task.complexity or self.model_selector.classify(task)
        task.complexity = complexity
        agents = self._parallel_code_agents()
        if len(agents) < 2:
            return None
        profile = hints.get("normalized_text_profile") if isinstance(hints, dict) else None
        explicit = bool(hints.get("parallelize_code"))
        profile_parallel = isinstance(profile, dict) and str(profile.get("execution_shape") or "") == "parallel_candidate"
        looks_large = len(task.input.files) > 1 or len(task.input.acceptance_criteria) > 1 or len(task.input.description.strip()) >= 80
        if not explicit and not profile_parallel and complexity not in {Complexity.HIGH, Complexity.CRITICAL} and not looks_large:
            return None
        socraticode_hint = hints.get("socraticode") if isinstance(hints.get("socraticode"), dict) else {}
        socraticode_coverage = hints.get("socraticode_context_coverage") if isinstance(hints.get("socraticode_context_coverage"), dict) else {}
        if not socraticode_coverage and isinstance(socraticode_hint.get("context_coverage"), dict):
            socraticode_coverage = socraticode_hint.get("context_coverage")
        socraticode_parallelism = hints.get("socraticode_parallelism") if isinstance(hints.get("socraticode_parallelism"), dict) else {}
        socraticode_routing = socraticode_hint.get("routing_recommendations") if isinstance(socraticode_hint.get("routing_recommendations"), dict) else {}
        socraticode_score = socraticode_coverage.get("score")
        if socraticode_score is None:
            socraticode_score = socraticode_coverage.get("coverage_ratio")
        if socraticode_score is None:
            socraticode_score = socraticode_coverage.get("ratio")
        try:
            socraticode_score = float(socraticode_score)
        except (TypeError, ValueError):
            socraticode_score = 0.0
        socraticode_status = str(socraticode_coverage.get("status") or "").strip().lower()
        suggested_parallel_branches = socraticode_parallelism.get("recommended_parallel_branches")
        if suggested_parallel_branches is None:
            suggested_parallel_branches = socraticode_routing.get("reduce_parallel_branches_to")

        max_branches_raw = str(hints.get("parallel_branches") or "").strip()
        if max_branches_raw.isdigit():
            max_branches = max(2, int(max_branches_raw))
        else:
            try:
                max_branches = max(2, int(__import__("os").getenv("AI_BRIDGE_PARALLEL_CODE_BRANCHES_MAX", "10")))
            except ValueError:
                max_branches = 10
        try:
            suggested_parallel_branches = int(suggested_parallel_branches)
        except (TypeError, ValueError):
            suggested_parallel_branches = None
        if suggested_parallel_branches is not None and suggested_parallel_branches >= 2 and (socraticode_score >= 0.9 or socraticode_status in {"strong", "good"}):
            max_branches = min(max_branches, suggested_parallel_branches)
        selected_agents = agents[:min(len(agents), max_branches)]
        branch_templates = self._openai_template_candidates(advisory_context, "code_parallel")
        review_templates = self._openai_template_candidates(advisory_context, "review_primary")
        branch_labels = ["primary", "fast", "safe", "alt", "review-ready", "fallback", "backend", "frontend", "infra", "stability"]
        branches: list[Task] = []
        for idx, agent_id in enumerate(selected_agents):
            label = branch_labels[idx] if idx < len(branch_labels) else f"branch-{idx+1}"
            template = branch_templates[idx] if idx < len(branch_templates) else {}
            branch_hints = {
                **dict(hints),
                "preferred_agent_id": agent_id,
                "batch_forced_agent_id": agent_id,
                "parallelize_code": True,
                "parallel_group": "code_fanout",
                "fanout_label": label,
            }
            requested_model = str(template.get("model_name") or "").strip()
            preferred_provider = str(template.get("provider") or "").strip().lower()
            if requested_model:
                branch_hints["requested_model"] = requested_model
                branch_hints["model_template_role"] = str(template.get("role") or "code_parallel")
                branch_hints["model_template_family"] = str(template.get("family") or "")
                branch_hints["model_template_tier"] = str(template.get("tier") or "")
            if preferred_provider:
                branch_hints["preferred_provider"] = preferred_provider
            branch = Task(
                TaskType.CODE,
                TaskInput(
                    f"[{label}] {task.input.description}",
                    files=list(task.input.files),
                    constraints=list(task.input.constraints),
                    acceptance_criteria=list(task.input.acceptance_criteria),
                ),
                task.context,
                Priority.NORMAL if task.priority == Priority.NORMAL else task.priority,
                parent_task_id=task.task_id,
                draft_layer=f"parallel_code_{label}",
                routing_hints=branch_hints,
            )
            if requested_model:
                branch.assigned_model = requested_model
            branch.required_capability = "code"
            branches.append(branch)

        review_hints = {**dict(hints), "parallel_source": "code_fanout"}
        review_model = str((review_templates[0] if review_templates else {}).get("model_name") or "").strip()
        review_provider = str((review_templates[0] if review_templates else {}).get("provider") or "").strip().lower()
        if review_model:
            review_hints["requested_model"] = review_model
            review_hints["preferred_provider"] = review_provider or "openai"
            review_hints["model_template_role"] = "review_primary"
        review = Task(
            TaskType.REVIEW,
            TaskInput(
                f"Review and consolidate parallel implementations for: {task.input.description}",
                files=list(task.input.files),
                acceptance_criteria=["best implementation selected", "tradeoffs documented"],
            ),
            task.context,
            Priority.HIGH if task.priority in {Priority.HIGH, Priority.CRITICAL} else Priority.NORMAL,
            parent_task_id=task.task_id,
            dependencies=[branch.task_id for branch in branches],
            draft_layer="parallel_code_review",
            routing_hints=review_hints,
        )
        if review_model:
            review.assigned_model = review_model
        review.required_capability = "review"
        tasks = [*branches, review]
        for atomic in tasks:
            self._decorate(atomic)
        return ExecutionPlan(root_task_id=task.task_id, atomic_tasks=tasks, draft_layers=[{"name": "parallel_code", "objective": task.input.description, "capability": "code", "task_type": "code", "parallel": True}])

    def _draft_layers_to_plan(self, task: Task, draft: dict[str, Any]) -> ExecutionPlan:
        layers = draft.get("layers") if isinstance(draft.get("layers"), list) else []
        if not layers:
            return self._default_plan(task)

        draft_layers: list[dict[str, Any]] = []
        tasks: list[Task] = []
        id_by_layer: dict[str, str] = {}
        layer_task_ids: dict[str, list[str]] = {}
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
                    agent_capability = self._capability_from_agent_hint(agent_hint, capability)
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
                            "parallel_group": True,
                            "source": "local_llm" if draft.get("status") == "model" else "heuristic",
                        },
                    )
                    agent_atomic.required_capability = agent_capability
                    id_by_layer[f"{layer_name}_{agent_hint}"] = agent_atomic.task_id
                    layer_task_ids[f"{layer_name}_{agent_hint}"] = [agent_atomic.task_id]
                    group_task_ids.append(agent_atomic.task_id)
                    tasks.append(agent_atomic)
                    
                    # Track dependencies for these group tasks later
                    pending_dependencies.append((agent_atomic, self._ensure_list(layer.get("dependencies"))))
                
                if group_task_ids:
                    id_by_layer[layer_name] = group_task_ids[0]
                    layer_task_ids[layer_name] = list(group_task_ids)
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
            atomic.routing_hints["parallel_group"] = bool(group_execution and len(sub_agents) > 1)
            id_by_layer[layer_name] = atomic.task_id
            layer_task_ids[layer_name] = [atomic.task_id]
            tasks.append(atomic)
            pending_dependencies.append((atomic, self._ensure_list(layer.get("dependencies"))))

        if not tasks:
            return self._default_plan(task)

        # Second pass dependency resolution.
        previous_task_id: str | None = None
        for atomic, deps in pending_dependencies:
            if deps:
                for dep in deps:
                    dep_name = dep.strip()
                    dep_ids = layer_task_ids.get(dep) or layer_task_ids.get(dep_name)
                    if not dep_ids:
                        fallback = id_by_layer.get(dep) or id_by_layer.get(dep_name)
                        dep_ids = [fallback] if fallback else []
                    for dep_id in dep_ids:
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

        return ExecutionPlan(root_task_id=task.task_id, atomic_tasks=tasks, draft_layers=draft_layers or layers)

    def _default_plan(self, task: Task) -> ExecutionPlan:
        context = task.context
        description = task.input.description
        plan_priority = task.priority
        review_priority = task.priority if task.priority in {Priority.HIGH, Priority.CRITICAL} else Priority.HIGH
        execution_priority = Priority.NORMAL

        plan = Task(TaskType.PLAN, TaskInput(f"Plan: {description}", acceptance_criteria=["execution plan created"]), context, plan_priority, parent_task_id=task.task_id)
        code = Task(TaskType.CODE, TaskInput(f"Implement: {description}", files=task.input.files, constraints=task.input.constraints, acceptance_criteria=task.input.acceptance_criteria), context, execution_priority, parent_task_id=task.task_id, dependencies=[plan.task_id])
        test = Task(TaskType.TEST, TaskInput(f"Test: {description}", files=task.input.files, acceptance_criteria=["tests pass"]), context, execution_priority, parent_task_id=task.task_id, dependencies=[code.task_id])
        review = Task(TaskType.REVIEW, TaskInput(f"Review: {description}", files=task.input.files, acceptance_criteria=["review pass"]), context, review_priority, parent_task_id=task.task_id, dependencies=[code.task_id])
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
                    "sub_agents": ["ux_planner"]
                },
                {
                    "name": "frontend_implementation",
                    "objective": f"Implement frontend components for {objective}",
                    "capability": "frontend",
                    "task_type": "code",
                    "dependencies": ["ux_design"],
                    "sub_agents": ["frontend_builder"]
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

        if task.type == TaskType.CODE:
            parallel_plan = self._parallel_code_plan(task, advisory_context=advisory_context)
            if parallel_plan is not None:
                return parallel_plan

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
