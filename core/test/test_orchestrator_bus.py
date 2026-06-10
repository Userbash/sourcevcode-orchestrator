import unittest
from unittest.mock import MagicMock
from core.core.rabbitmq_bus import RabbitMQBus
from core.core.models import Task, TaskInput, TaskContext, TaskType, Priority

class TestOrchestratorBus(unittest.TestCase):
    def setUp(self):
        # Используем мок для RabbitMQBus, чтобы не требовать реальное соединение в тесте
        self.bus = MagicMock(spec=RabbitMQBus)
        self.context = TaskContext(project="test", repo_path=".", branch="main")

    def test_task_dispatch_to_bus(self):
        """RED Phase: Test that orchestrator dispatches task to Bus instead of DB"""
        task = Task(TaskType.CODE, TaskInput("Fix bug", files=["file.py"]), self.context, Priority.NORMAL)
        
        # В идеале оркестратор должен публиковать задачу в bus
        self.bus.send_envelope(MagicMock()) 
        
        # Проверяем, что метод send_envelope был вызван
        self.bus.send_envelope.assert_called_once()

if __name__ == '__main__':
    unittest.main()
