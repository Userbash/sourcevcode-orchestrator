
import sys
sys.path.append('/var/home/sanya/wisper/core')
from agents.codex_agent import CodexAgent
from core.core.models import Priority, Task, TaskContext, TaskInput, TaskType

agent = CodexAgent()
task = Task(
    type=TaskType.CODE,
    input=TaskInput(
        description='Create a simple helper function `get_timestamp()` in a new file `core/core/utils/time_utils.py` that returns the current UTC timestamp as a string.',
        files=[],
        constraints=['Use standard library only', 'Follow PEP8'],
        acceptance_criteria=['The file exists', 'The function returns a valid ISO timestamp string']
    ),
    context=TaskContext('demo', '.', 'main'),
    priority=Priority.NORMAL,
    assigned_model='gpt-4o'
)
result = agent.run(task)
print(result.output.summary)
