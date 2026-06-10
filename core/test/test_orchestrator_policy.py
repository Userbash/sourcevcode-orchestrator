import unittest
from core.core.models import Task, TaskInput, TaskContext, TaskType, Complexity, Priority
from core.core.model_selector import ModelSelector

class TestOrchestrationPolicy(unittest.TestCase):
    def setUp(self):
        self.selector = ModelSelector()
        self.context = TaskContext(project="test", repo_path=".", branch="main")

    def test_security_keyword_escalation(self):
        # Risk Detection: security/auth -> HIGH -> Cloud
        task = Task(TaskType.CODE, TaskInput("Implement OAuth flow", files=["auth.py"]), self.context, Priority.NORMAL)
        choice = self.selector.select(task)
        self.assertIn("openai", choice.provider)

    def test_complex_code_escalation(self):
        # Complexity HIGH -> Cloud
        task = Task(TaskType.CODE, TaskInput("Distributed architecture implementation", files=["a.py", "b.py", "c.py"]), self.context, Priority.HIGH)
        choice = self.selector.select(task)
        self.assertIn("openai", choice.provider)

    def test_local_efficiency(self):
        # LOW complexity -> Local
        task = Task(TaskType.PLAN, TaskInput("Planning simple task"), self.context, Priority.NORMAL)
        choice = self.selector.select(task)
        self.assertEqual(choice.provider, "local")

if __name__ == '__main__':
    unittest.main()
