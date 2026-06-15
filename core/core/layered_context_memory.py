from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from .models import AgentResult, ExecutionPlan, Task
from .persistent_memory import PersistentMemoryManager
from .session_memory import SessionMemory


LAYER_INTENT = "ctx:intent"
LAYER_NORMALIZED = "ctx:normalized_task"
LAYER_PLANNING = "ctx:planning_draft"
LAYER_DECOMPOSITION = "ctx:decomposition"
LAYER_EXECUTION_PROMPT = "ctx:execution_prompt"
LAYER_RESULT = "ctx:result_summary"
LAYER_ROUTING = "ctx:routing_outcome"
LAYER_QUALITY = "ctx:quality_outcome"
LAYER_LESSON = "ctx:lessons_learned"

PROMPT_DOMAIN_PREFIX = "layered_prompt:"
ROUTING_DOMAIN_PREFIX = "layered_routing:"
EXECUTION_DOMAIN_PREFIX = "layered_execution:"


@dataclass(slots=True)
class LayeredContextSlice:
    intent: dict[str, Any] | None
    normalized_task: dict[str, Any] | None
    planning_draft: dict[str, Any] | None
    decomposition: dict[str, Any] | None
    routing_outcome: dict[str, Any] | None
    quality_outcome: dict[str, Any] | None
    result_summary: dict[str, Any] | None
    lessons: list[dict[str, Any]]
    prompt_examples: list[dict[str, Any]]
    layered_context_brief: str
    prompt_memory_brief: str
    routing_memory_brief: str
    execution_memory_brief: str
    prompt_guidance: list[str]


class LayeredContextMemory:
    def __init__(self, session_memory: SessionMemory) -> None:
        self.session_memory = session_memory
        self.hybrid = session_memory.hybrid
        self.persistent: PersistentMemoryManager = session_memory.hybrid.persistent

    @staticmethod
    def _task_type(task: Task) -> str:
        return str(getattr(getattr(task, 'type', None), 'value', getattr(task, 'type', 'unknown')) or 'unknown').lower()

    @staticmethod
    def _task_payload(task: Task) -> dict[str, Any]:
        return {
            'task_id': task.task_id,
            'task_type': LayeredContextMemory._task_type(task),
            'description': task.input.description,
            'files': list(task.input.files),
            'constraints': list(task.input.constraints),
            'acceptance_criteria': list(task.input.acceptance_criteria),
            'priority': getattr(task.priority, 'value', str(task.priority)),
            'session_id': task.session_id,
            'assigned_model': task.assigned_model,
            'required_capability': task.required_capability,
        }

    @staticmethod
    def _jsonable(value: Any) -> Any:
        try:
            json.dumps(value, ensure_ascii=True, default=str)
            return value
        except Exception:
            return str(value)

    @staticmethod
    def _truncate(text: str, limit: int = 1200) -> str:
        text = str(text or '').strip()
        if len(text) <= limit:
            return text
        return text[: max(0, limit - 3)].rstrip() + '...'

    def _store_layer(
        self,
        *,
        session_id: str,
        agent_id: str,
        memory_type: str,
        content: dict[str, Any],
        importance_score: float,
        metadata: dict[str, Any],
    ) -> int:
        return self.persistent.store_memory(
            session_id=session_id,
            agent_id=agent_id,
            memory_type=memory_type,
            content=self._jsonable(content),
            importance_score=importance_score,
            metadata={k: self._jsonable(v) for k, v in metadata.items()},
        )

    def record_submission(self, task: Task, *, raw_payload: Any, normalized_payload: dict[str, Any], source: str) -> None:
        session_id = task.session_id or task.task_id
        base_meta = {
            'task_id': task.task_id,
            'task_type': self._task_type(task),
            'source': source,
            'stage': 'submission',
        }
        self._store_layer(
            session_id=session_id,
            agent_id='orchestrator',
            memory_type=LAYER_INTENT,
            content={
                'task': self._task_payload(task),
                'raw_user_intent': self._jsonable(raw_payload),
            },
            importance_score=0.95,
            metadata=base_meta,
        )
        self._store_layer(
            session_id=session_id,
            agent_id='orchestrator',
            memory_type=LAYER_NORMALIZED,
            content={
                'task': self._task_payload(task),
                'normalized_task': self._jsonable(normalized_payload),
            },
            importance_score=0.9,
            metadata=base_meta,
        )

    def record_planning_draft(self, task: Task, advisory_context: dict[str, Any] | None, *, source: str = 'orchestrator') -> None:
        advisory_context = advisory_context or {}
        local = advisory_context.get('local_llm') if isinstance(advisory_context.get('local_llm'), dict) else {}
        mimo = advisory_context.get('mimo') if isinstance(advisory_context.get('mimo'), dict) else {}
        decomposition = local.get('decomposition') if isinstance(local.get('decomposition'), dict) else None
        summary = local.get('summary') or mimo.get('task_profile', {}).get('name') or ''
        if not decomposition and not summary:
            return
        session_id = task.session_id or task.task_id
        content = {
            'task': self._task_payload(task),
            'summary': self._truncate(str(summary or ''), 600),
            'decomposition_draft': self._jsonable(decomposition or {}),
            'budget_pressure': mimo.get('budget_pressure'),
            'context_depth': mimo.get('context_depth'),
            'provider_health': mimo.get('provider_health'),
        }
        self._store_layer(
            session_id=session_id,
            agent_id='orchestrator',
            memory_type=LAYER_PLANNING,
            content=content,
            importance_score=0.82,
            metadata={'task_id': task.task_id, 'task_type': self._task_type(task), 'source': source, 'stage': 'planning'},
        )

    def record_decomposition(self, task: Task, plan: ExecutionPlan, *, source: str) -> None:
        session_id = task.session_id or task.task_id
        tasks = []
        for item in plan.atomic_tasks:
            tasks.append({
                'task_id': item.task_id,
                'task_type': self._task_type(item),
                'objective': item.input.description,
                'dependencies': list(item.dependencies),
                'required_capability': item.required_capability,
                'assigned_model': item.assigned_model,
                'routing_hints': dict(item.routing_hints or {}),
            })
        self._store_layer(
            session_id=session_id,
            agent_id='orchestrator',
            memory_type=LAYER_DECOMPOSITION,
            content={
                'root_task_id': task.task_id,
                'task_type': self._task_type(task),
                'draft_layers': self._jsonable(plan.draft_layers),
                'decomposed_tasks': tasks,
                'parallel_tasks': sum(1 for item in tasks if item.get('routing_hints', {}).get('parallel_group')),
            },
            importance_score=0.88,
            metadata={'task_id': task.task_id, 'task_type': self._task_type(task), 'source': source, 'stage': 'decomposition', 'atomic_count': len(tasks)},
        )

    def record_routing_outcome(
        self,
        task: Task,
        *,
        selected_provider: str,
        selected_model: str,
        routed_agent: str,
        routed_provider: str,
        routed_model: str,
        reason: str,
        fallback_count: int,
    ) -> None:
        session_id = task.session_id or task.task_id
        self._store_layer(
            session_id=session_id,
            agent_id='orchestrator',
            memory_type=LAYER_ROUTING,
            content={
                'task': self._task_payload(task),
                'selected_provider': selected_provider,
                'selected_model': selected_model,
                'routed_agent': routed_agent,
                'routed_provider': routed_provider,
                'routed_model': routed_model,
                'fallback_count': int(fallback_count),
                'reason': self._truncate(reason, 400),
            },
            importance_score=0.86,
            metadata={'task_id': task.task_id, 'task_type': self._task_type(task), 'stage': 'routing', 'provider': routed_provider, 'model_name': routed_model},
        )

    def record_execution_prompt(self, task: Task, *, agent_id: str, provider: str, model_name: str, prompt: str, memory_context: dict[str, Any] | None = None) -> None:
        session_id = task.session_id or task.task_id
        memory_context = memory_context or {}
        content = {
            'task': self._task_payload(task),
            'provider': provider,
            'model_name': model_name,
            'execution_prompt': prompt,
            'memory_layers_used': {
                'trained_memory': bool(memory_context.get('trained_memory_brief')),
                'reusable_task_memory': bool(memory_context.get('reusable_task_memory_brief')),
                'layered_context': bool(memory_context.get('layered_context_brief')),
                'handoffs': bool(memory_context.get('handoff_summaries')),
            },
        }
        self._store_layer(
            session_id=session_id,
            agent_id=agent_id,
            memory_type=LAYER_EXECUTION_PROMPT,
            content=content,
            importance_score=0.78,
            metadata={'task_id': task.task_id, 'task_type': self._task_type(task), 'stage': 'execution_prompt', 'provider': provider, 'model_name': model_name, 'prompt_chars': len(prompt)},
        )

    def _derive_lessons(self, task: Task, result: AgentResult, quality_score: float, *, provider: str, model_name: str, fallback_count: int) -> list[str]:
        lessons: list[str] = []
        if task.input.acceptance_criteria:
            lessons.append('keep acceptance criteria explicit in the final prompt')
        if task.input.files:
            lessons.append('list impacted files directly in the prompt when code or review work is requested')
        if task.type.value in {'plan', 'code'} and len(task.input.description) > 80:
            lessons.append('preserve a planning draft before decomposition for large tasks')
        if fallback_count > 0:
            lessons.append('prefer a provider fallback chain when the first routed agent degrades or fails')
        if result.status.value == 'done' and quality_score >= 0.75:
            lessons.append(f'{provider}/{model_name} produced an acceptable result for this task family')
        if result.status.value != 'done':
            lessons.append(f'capture the failed prompt and routing path for future avoidance on {provider}/{model_name}')
        if task.type.value in {'review', 'test'}:
            lessons.append('request concise verdicts, concrete issues, and next corrective steps')
        return lessons[:6]

    def record_result(self, task: Task, result: AgentResult, *, quality_score: float, fallback_count: int, latency_ms: float) -> None:
        session_id = task.session_id or task.task_id
        provider = str(result.provider or 'unknown')
        model_name = str(result.model_name or 'unknown')
        summary = self._truncate(str(result.output.get('summary', '') or ''), 1200)
        result_payload = {
            'task': self._task_payload(task),
            'status': result.status.value,
            'provider': provider,
            'model_name': model_name,
            'summary': summary,
            'errors': [self._truncate(str(item), 300) for item in (result.errors or [])[:6]],
            'next_recommendations': [self._truncate(str(item), 200) for item in (result.next_recommendations or [])[:6]],
            'latency_ms': round(float(latency_ms), 3),
        }
        self._store_layer(
            session_id=session_id,
            agent_id=result.agent_id,
            memory_type=LAYER_RESULT,
            content=result_payload,
            importance_score=0.88 if result.status.value == 'done' else 0.65,
            metadata={'task_id': task.task_id, 'task_type': self._task_type(task), 'stage': 'result', 'provider': provider, 'model_name': model_name},
        )
        self._store_layer(
            session_id=session_id,
            agent_id=result.agent_id,
            memory_type=LAYER_QUALITY,
            content={
                'task_id': task.task_id,
                'task_type': self._task_type(task),
                'provider': provider,
                'model_name': model_name,
                'quality_score': max(0.0, min(1.0, float(quality_score))),
                'status': result.status.value,
                'fallback_count': int(fallback_count),
            },
            importance_score=0.9,
            metadata={'task_id': task.task_id, 'task_type': self._task_type(task), 'stage': 'quality', 'provider': provider, 'model_name': model_name},
        )
        lessons = self._derive_lessons(task, result, quality_score, provider=provider, model_name=model_name, fallback_count=fallback_count)
        if lessons:
            lesson_payload = {
                'task_id': task.task_id,
                'task_type': self._task_type(task),
                'provider': provider,
                'model_name': model_name,
                'lessons': lessons,
                'summary': summary,
                'quality_score': max(0.0, min(1.0, float(quality_score))),
            }
            self._store_layer(
                session_id=session_id,
                agent_id=result.agent_id,
                memory_type=LAYER_LESSON,
                content=lesson_payload,
                importance_score=0.92,
                metadata={'task_id': task.task_id, 'task_type': self._task_type(task), 'stage': 'lesson', 'provider': provider, 'model_name': model_name},
            )
            score = max(0.0, min(1.0, float(quality_score)))
            self.persistent.store_trained_memory(
                session_id=session_id,
                agent_id=result.agent_id,
                memory_domain=f'{PROMPT_DOMAIN_PREFIX}{self._task_type(task)}',
                content={'provider': provider, 'model_name': model_name, 'summary': summary, 'lessons': lessons, 'task': self._task_payload(task)},
                metadata={'task_type': self._task_type(task), 'provider': provider, 'model_name': model_name, 'source': 'layered_context_memory'},
                quality_score=score,
            )
            self.persistent.store_trained_memory(
                session_id=session_id,
                agent_id=result.agent_id,
                memory_domain=f'{ROUTING_DOMAIN_PREFIX}{self._task_type(task)}',
                content={'provider': provider, 'model_name': model_name, 'status': result.status.value, 'fallback_count': fallback_count},
                metadata={'task_type': self._task_type(task), 'provider': provider, 'model_name': model_name, 'source': 'layered_context_memory'},
                quality_score=score,
            )
            self.persistent.store_trained_memory(
                session_id=session_id,
                agent_id=result.agent_id,
                memory_domain=f'{EXECUTION_DOMAIN_PREFIX}{self._task_type(task)}',
                content={'provider': provider, 'model_name': model_name, 'status': result.status.value, 'summary': summary, 'errors': list(result.errors or [])[:4]},
                metadata={'task_type': self._task_type(task), 'provider': provider, 'model_name': model_name, 'source': 'layered_context_memory'},
                quality_score=score,
            )

    def _latest_by_type(self, records: list[Any], memory_type: str) -> dict[str, Any] | None:
        for record in records:
            if str(getattr(record, 'memory_type', '')) == memory_type:
                content = getattr(record, 'content', None)
                return content if isinstance(content, dict) else None
        return None

    def _trained_examples(self, *, task_type: str, provider: str, prefix: str, limit: int = 3) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        for record in self.persistent.list_trained_memories(limit=200):
            domain = str(getattr(record, 'memory_domain', '') or '')
            if domain != f'{prefix}{task_type}':
                continue
            metadata = getattr(record, 'metadata', {}) or {}
            if provider and metadata.get('provider') not in {'', provider}:
                continue
            content = getattr(record, 'content', None)
            if isinstance(content, dict):
                items.append(content)
            if len(items) >= limit:
                break
        return items

    def build_context_pie(self, task: Task, *, agent_id: str, provider: str = '', model_name: str = '', token_limit: int = 220) -> LayeredContextSlice:
        session_id = task.session_id or task.task_id
        records = self.persistent.list_session_memories(session_id=session_id, memory_type_prefix='ctx:', limit=120)
        intent = self._latest_by_type(records, LAYER_INTENT)
        normalized_task = self._latest_by_type(records, LAYER_NORMALIZED)
        planning = self._latest_by_type(records, LAYER_PLANNING)
        decomposition = self._latest_by_type(records, LAYER_DECOMPOSITION)
        routing = self._latest_by_type(records, LAYER_ROUTING)
        quality = self._latest_by_type(records, LAYER_QUALITY)
        result = self._latest_by_type(records, LAYER_RESULT)
        lessons = [getattr(record, 'content', {}) for record in records if str(getattr(record, 'memory_type', '')) == LAYER_LESSON and isinstance(getattr(record, 'content', None), dict)][:3]
        prompt_examples = self._trained_examples(task_type=self._task_type(task), provider=provider, prefix=PROMPT_DOMAIN_PREFIX)
        routing_examples = self._trained_examples(task_type=self._task_type(task), provider=provider, prefix=ROUTING_DOMAIN_PREFIX)
        execution_examples = self._trained_examples(task_type=self._task_type(task), provider=provider, prefix=EXECUTION_DOMAIN_PREFIX)

        prompt_guidance: list[str] = []
        if task.input.acceptance_criteria:
            prompt_guidance.append('repeat acceptance criteria verbatim in the working prompt')
        if task.input.files:
            prompt_guidance.append('name concrete files before asking the model to edit or review code')
        if planning and planning.get('summary'):
            prompt_guidance.append('preserve the planning summary before expanding into subtasks')
        for item in lessons:
            prompt_guidance.extend([str(lesson).strip() for lesson in item.get('lessons', []) if str(lesson).strip()])
        prompt_guidance = list(dict.fromkeys(prompt_guidance))[:6]

        budget_chars = max(400, token_limit * 4)
        lines: list[str] = []
        if normalized_task and isinstance(normalized_task.get('normalized_task'), dict):
            norm = normalized_task.get('normalized_task') or {}
            desc = str(norm.get('description') or norm.get('message') or task.input.description)
            lines.append(f'NORMALIZED TASK: {self._truncate(desc, 240)}')
        if planning and planning.get('summary'):
            lines.append(f'PLANNING DRAFT: {self._truncate(str(planning.get("summary") or ""), 200)}')
        if decomposition and decomposition.get('decomposed_tasks'):
            tasks = decomposition.get('decomposed_tasks') or []
            lines.append(f'DECOMPOSITION: {len(tasks)} atomic tasks prepared')
        if routing and routing.get('routed_provider'):
            lines.append(f'ROUTING MEMORY: preferred provider={routing.get("routed_provider")} model={routing.get("routed_model")} fallback_count={routing.get("fallback_count", 0)}')
        if result and result.get('summary'):
            lines.append(f'LAST RESULT: {self._truncate(str(result.get("summary") or ""), 220)}')
        if quality and quality.get('quality_score') is not None:
            lines.append(f'QUALITY MEMORY: score={float(quality.get("quality_score") or 0.0):.2f} status={quality.get("status") or "unknown"}')
        for item in prompt_examples[:2]:
            lines.append(f'PROMPT EXAMPLE: {self._truncate(str(item.get("summary") or ""), 180)}')
        for item in routing_examples[:1]:
            lines.append(f'ROUTING LESSON: provider={item.get("provider")} model={item.get("model_name")} fallback_count={item.get("fallback_count", 0)}')
        for item in execution_examples[:1]:
            lines.append(f'EXECUTION LESSON: {self._truncate(str(item.get("summary") or ""), 180)}')
        if prompt_guidance:
            lines.extend([f'PROMPT GUIDANCE: {item}' for item in prompt_guidance])

        brief_lines: list[str] = []
        used = 0
        for line in lines:
            if not line:
                continue
            if used + len(line) + 1 > budget_chars:
                break
            brief_lines.append(line)
            used += len(line) + 1
        layered_context_brief = "\n".join(brief_lines)
        return LayeredContextSlice(
            intent=intent,
            normalized_task=normalized_task,
            planning_draft=planning,
            decomposition=decomposition,
            routing_outcome=routing,
            quality_outcome=quality,
            result_summary=result,
            lessons=lessons,
            prompt_examples=prompt_examples,
            layered_context_brief=layered_context_brief,
            prompt_memory_brief="\n".join([self._truncate(str(item.get('summary') or ''), 160) for item in prompt_examples[:2] if str(item.get('summary') or '').strip()]),
            routing_memory_brief="\n".join([self._truncate(str(item), 200) for item in prompt_guidance[:2]]),
            execution_memory_brief="\n".join([self._truncate(str(item.get('summary') or ''), 180) for item in execution_examples[:2] if str(item.get('summary') or '').strip()]),
            prompt_guidance=prompt_guidance,
        )
