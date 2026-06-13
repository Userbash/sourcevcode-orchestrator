import os
import argparse
from core.core.orchestrator_interface import TaskOrchestrationInterface


def verify_parallel_dag():
    parser = argparse.ArgumentParser()
    parser.add_argument("--verbose-orchestrator", action="store_true")
    parser.add_argument("--json-console", action="store_true")
    parser.add_argument("--log-file", default=os.getenv("ORCHESTRATOR_LOG_FILE", ""))
    args = parser.parse_args()

    # Disable autostart to keep the test fast
    os.environ["AI_BRIDGE_AUTOSTART_LOCAL_LLM"] = "false"
    os.environ["AI_BRIDGE_AUTO_BOOTSTRAP"] = "false"
    if args.log_file:
        os.environ["ORCHESTRATOR_LOG_FILE"] = args.log_file

    interface = TaskOrchestrationInterface(verbose_orchestrator=args.verbose_orchestrator, json_console=args.json_console)

    print("--- Testing Parallel DAG Orchestration ---")
    print(f"verbose_orchestrator={args.verbose_orchestrator} json_console={args.json_console} log_file={args.log_file or 'off'}")

    # Run the complex task
    # This will trigger: Draft -> Decompose -> TDD injection -> Readability injection -> Parallel Run
    result = interface.execute_complex_task("BUILD: Форма авторизации", verbose_orchestrator=args.verbose_orchestrator, json_console=args.json_console)

    print(f"\nFinal Execution Status: {result.get('status')}")

if __name__ == "__main__":
    verify_parallel_dag()
