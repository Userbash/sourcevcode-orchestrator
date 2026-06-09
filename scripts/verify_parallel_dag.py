import os
from core.core.orchestrator_interface import TaskOrchestrationInterface

def verify_parallel_dag():
    # Disable autostart to keep the test fast
    os.environ["AI_BRIDGE_AUTOSTART_LOCAL_LLM"] = "false"
    os.environ["AI_BRIDGE_AUTO_BOOTSTRAP"] = "false"
    
    interface = TaskOrchestrationInterface()
    
    print("--- Testing Parallel DAG Orchestration ---")
    
    # Run the complex task
    # This will trigger: Draft -> Decompose -> TDD injection -> Readability injection -> Parallel Run
    result = interface.execute_complex_task("BUILD: Форма авторизации")
    
    print(f"\nFinal Execution Status: {result.get('status')}")

if __name__ == "__main__":
    verify_parallel_dag()
