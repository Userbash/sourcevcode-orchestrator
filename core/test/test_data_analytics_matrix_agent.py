from core.agents.data_analytics_matrix_agent import DataAnalyticsMatrixAgent
from core.core.models import Task, TaskContext, TaskInput, TaskType


def test_data_analytics_matrix_agent_returns_matrix_output_and_knowledge_pool():
    agent = DataAnalyticsMatrixAgent()
    task = Task(
        task_id="matrix-agent",
        type=TaskType.CODE,
        input=TaskInput(
            description="Build analytics matrix agent for retrieval and data science search",
            files=["core/core/analytics_matrix_engine.py"],
        ),
        context=TaskContext(project="core", repo_path="."),
    )

    result = agent.run(task, memory_context={"trained_memory_brief": "Earlier work covered retrieval ranking and provider policy."})

    assert result.output.summary
    assert result.output.analytics_matrix["keywords"]
    assert result.output.knowledge_pool["record_count"] == 1
    assert "retrieval" in result.output.analytics_matrix["keywords"]
