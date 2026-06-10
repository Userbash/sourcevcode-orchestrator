from core.core.models import Task, TaskInput, TaskContext, TaskType, Priority
from core.core.model_selector import ModelSelector
import json

def verify_routing():
    selector = ModelSelector()
    context = TaskContext(project="test", repo_path=".", branch="main")
    
    # Task: "напиши тест для модели"
    task = Task(TaskType.CODE, TaskInput("напиши тест для модели"), context, Priority.NORMAL)
    
    choice = selector.select(task)
    
    print(f"--- Verification Result ---")
    print(f"Task Type: {task.type}")
    print(f"Provider: {choice.provider}")
    print(f"Model: {choice.model_name}")
    print(f"Params (Temperature): {choice.params.temperature}")
    print(f"Reason: {choice.reason}")
    
    # Assertions
    assert choice.provider == "local"
    assert choice.model_name == "qwen2.5-coder:14b"
    assert choice.params.temperature == 0.2
    print("--- SUCCESS: Routing Policy Verified ---")

if __name__ == '__main__':
    verify_routing()
