from core.core.feedback_loop import FeedbackLoop
from core.core.models import AgentResult, ResultOutput, Task, TaskContext, TaskInput, TaskStatus, TaskType


def test_feedback_creates_fix_task_on_failure():
    task = Task(TaskType.CODE, TaskInput("write code"), TaskContext("p", ".", "main"))
    result = AgentResult(task_id=task.task_id, agent_id="codex", status=TaskStatus.FAILED, output=ResultOutput(summary="failed", files_changed=[], commands_run=[], test_results=[], diff=""), confidence=0.2, errors=["test failed"], next_recommendations=[], provider="openai", model_name="gpt-4o")

    clone = AgentResult.model_validate(result.model_dump())
    assert clone.provider == "openai"
    assert clone.model_name == "gpt-4o"
    assert clone.output.summary == "failed"

    ok, fix = FeedbackLoop(retry_limit=1).evaluate(task, result)

    assert not ok
    assert fix is not None
    assert fix.type == TaskType.FIX
    assert fix.required_capability == "fix"
