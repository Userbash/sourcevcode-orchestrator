from __future__ import annotations

from core.core.layered_context_memory import LayeredContextMemory
from core.core.models import ExecutionPlan, ResultOutput, Task, TaskContext, TaskInput, TaskStatus, TaskType, AgentResult
from core.core.session_memory import SessionMemory


def _task() -> Task:
    task = Task(
        TaskType.CODE,
        TaskInput('Implement layered prompt memory for orchestrator', files=['core/core/orchestrator.py'], constraints=['do not break handlers'], acceptance_criteria=['tests pass', 'prompt memory is reusable']),
        TaskContext('demo', '.', 'main'),
    )
    task.session_id = 'sess-layered'
    task.required_capability = 'code'
    return task


def test_layered_context_records_submission_and_retrieves_pie(tmp_path, monkeypatch):
    monkeypatch.setenv('AI_BRIDGE_MEMORY_STORE_DIR', str(tmp_path))
    memory = SessionMemory()
    layered = LayeredContextMemory(memory)
    task = _task()

    layered.record_submission(task, raw_payload='build feature', normalized_payload={'description': task.input.description, 'files': task.input.files}, source='user_input')
    layered.record_planning_draft(task, {'local_llm': {'summary': 'Plan first, then decompose', 'decomposition': {'layers': [{'name': 'code', 'objective': 'implement feature'}]}}}, source='test')
    plan = ExecutionPlan(root_task_id=task.task_id, atomic_tasks=[task], draft_layers=[{'name': 'code', 'objective': task.input.description}])
    layered.record_decomposition(task, plan, source='test')
    layered.record_routing_outcome(task, selected_provider='antigravity', selected_model='gemini-3.5-flash', routed_agent='codex-main', routed_provider='antigravity', routed_model='gemini-3.5-flash', reason='policy', fallback_count=0)
    layered.record_execution_prompt(task, agent_id='codex-main', provider='antigravity', model_name='gemini-3.5-flash', prompt='OBJECTIVE: Implement feature', memory_context={'trained_memory_brief': 'Quality: prior prompt worked'})
    result = AgentResult(task.task_id, 'codex-main', TaskStatus.DONE, ResultOutput(summary='Implemented successfully'), 0.9, [], [], provider='antigravity', model_name='gemini-3.5-flash')
    layered.record_result(task, result, quality_score=0.91, fallback_count=0, latency_ms=1250.0)

    pie = layered.build_context_pie(task, agent_id='codex-main', provider='antigravity', model_name='gemini-3.5-flash')

    assert pie.normalized_task is not None
    assert 'NORMALIZED TASK:' in pie.layered_context_brief
    assert any('acceptance criteria' in item for item in pie.prompt_guidance)
    assert pie.prompt_examples


def test_layered_context_uses_persistent_session_listing(tmp_path, monkeypatch):
    monkeypatch.setenv('AI_BRIDGE_MEMORY_STORE_DIR', str(tmp_path))
    memory = SessionMemory()
    task = _task()
    layered = memory.layered
    layered.record_submission(task, raw_payload={'message': 'hello'}, normalized_payload={'description': task.input.description}, source='api')

    rows = memory.hybrid.persistent.list_session_memories(session_id=task.session_id or task.task_id, memory_type_prefix='ctx:', limit=10)

    assert rows
    assert rows[0].memory_type.startswith('ctx:')
