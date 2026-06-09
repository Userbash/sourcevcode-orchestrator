from __future__ import annotations

import json
import logging
import os
import subprocess
import time
from dataclasses import dataclass
from typing import Any, List, Optional

from .kernel_protocol import KernelAPI, KernelModule
from .models import Task, TaskType, TaskStatus, AgentResult, ResultOutput

logger = logging.getLogger("qwen_code_module")

@dataclass
class QwenConfig:
    binary_path: str = "qwen"
    default_model: str = os.getenv("AI_BRIDGE_LOCAL_LLM_MODEL") or os.getenv("QWEN_CODE_MODEL") or "qwen2.5-coder:32b-instruct-q3_k_m"
    timeout_sec: int = 300
    yolo_mode: bool = True


from .qwen_runtime_router import QwenRuntimeRouter

logger = logging.getLogger("qwen_code_module")

class QwenCodeModule(KernelModule):
    """
    Qwen Code integration module. 
    Wraps the @qwen-code/qwen-code CLI for high-performance code generation and analysis.
    """
    name: str = "qwen_code"
    _api: KernelAPI | None = None

    def __init__(self, config: Optional[QwenConfig] = None) -> None:
        self.config = config or QwenConfig()
        self._binary_path: str | None = None
        self.router = QwenRuntimeRouter()


    def on_load(self, api: KernelAPI) -> None:
        self._api = api
        self._api.log("info", f"[QWEN] Initializing {self.name} module...")
        
        # 1. Verification: Find binary
        self._binary_path = self._find_binary()
        if not self._binary_path:
            self._api.log("error", "[QWEN] Binary 'qwen' not found in PATH.")
            return

        # 2. Verification: Check version and health
        version = self._get_version()
        if version:
            self._api.log("info", f"[QWEN] Qwen Code version {version} detected and ready.")
        else:
            self._api.log("warning", "[QWEN] Could not verify Qwen Code health.")

    def on_unload(self) -> None:
        pass

    def _find_binary(self) -> Optional[str]:
        try:
            # Check custom npm path if standard which fails
            res = subprocess.run(["which", "qwen"], capture_output=True, text=True)
            if res.returncode == 0:
                return res.stdout.strip()
            
            # Fallback to local npm bin
            home_bin = os.path.expanduser("~/.npm-packages/bin/qwen")
            if os.path.exists(home_bin):
                return home_bin
        except Exception:
            pass
        return None

    def _get_version(self) -> Optional[str]:
        if not self._binary_path:
            return None
        try:
            res = subprocess.run([self._binary_path, "--version"], capture_output=True, text=True, timeout=5)
            return res.stdout.strip()
        except Exception:
            return None

    def query(self, prompt: str, model: Optional[str] = None, json_mode: bool = False) -> str:
        """Execute a non-interactive prompt via Qwen CLI with auto-auth detection."""
        if not self._binary_path:
            raise RuntimeError("Qwen binary not found.")

        cmd = [
            self._binary_path,
            "--prompt", prompt,
            "--model", model or self.config.default_model,
        ]
        
        # 1. Detect Auth & Base URL
        auth_type = "openai"
        qwen_key = os.getenv("QWEN_API_KEY")
        base_url = os.getenv("QWEN_OPENAI_BASE_URL") or os.getenv("AI_BRIDGE_LOCAL_LLM_ENDPOINT")
        
        if qwen_key:
            cmd.extend(["--openai-api-key", qwen_key])
            if os.getenv("QWEN_OPENAI_BASE_URL"):
                cmd.extend(["--openai-base-url", os.getenv("QWEN_OPENAI_BASE_URL")])
            cmd.extend(["--auth-type", "openai"])
        elif base_url:
            if "/v1" not in base_url and "11434" in base_url: # Ollama fix
                base_url = f"{base_url.rstrip('/')}/v1"
            cmd.extend(["--openai-base-url", base_url])
            cmd.extend(["--openai-api-key", os.getenv("OPENAI_API_KEY") or "ollama"])
            cmd.extend(["--auth-type", "openai"])
        else:
            # Fallback to cloud detection
            if os.getenv("OPENAI_API_KEY"):
                auth_type = "openai"
            elif os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY"):
                auth_type = "gemini"
            cmd.extend(["--auth-type", auth_type])

        
        if self.config.yolo_mode:
            cmd.append("--yolo")
        
        if json_mode:
            cmd.extend(["--output-format", "json"])

        # 2. Setup Environment
        env = os.environ.copy()
        env["QWEN_CODE_SUPPRESS_YOLO_WARNING"] = "1"


        try:
            start_time = time.perf_counter()
            process = subprocess.run(cmd, capture_output=True, text=True, timeout=self.config.timeout_sec, env=env)
            duration = time.perf_counter() - start_time
            
            if process.returncode != 0:
                # If fail, try to return stderr but filter warnings
                err = process.stderr.strip()
                if "No auth type is selected" in err and not auth_type:
                    return "ERROR: Authentication required but no API keys found in environment."
                return f"ERROR: {err}"

            return process.stdout.strip()
        except subprocess.TimeoutExpired:
            logger.error("[QWEN] CLI query timed out.")
            return "ERROR: Timeout"
        except Exception as e:
            logger.error(f"[QWEN] Query error: {e}")
            return f"ERROR: {str(e)}"


    def run_tdd_cycle(self, task: Task) -> AgentResult:
        """
        Specialized method for TDD:
        1. Write Test (if RED phase)
        2. Write Code (if GREEN phase)
        """
        # 1. Select Model via Router
        plan = self.router.build_plan(task)
        selected_model = plan.models[0]
        
        prompt = f"Task type: {task.type.value}. Objective: {task.input.description}. Files: {task.input.files}. Context: {task.context.as_dict()}"
        
        if task.type == TaskType.TEST:
            prompt += "\nINSTRUCTION: Write a failing test for this requirement. Ensure it captures the expected behavior but fails currently."
        elif task.type == TaskType.CODE:
            prompt += "\nINSTRUCTION: Implement the logic to make the existing tests pass. Follow strict SOLID principles."

        response = self.query(prompt, model=selected_model)
        
        # 2. Estimate and register usage
        # Simple heuristic: response length / 4
        usage = len(response) // 4 + plan.estimated_tokens
        self.router.register_usage(task.task_id, usage)
        
        # Parse response (simplification: assume success if no error string)
        if "ERROR:" in response:
            return AgentResult(task.task_id, self.name, TaskStatus.FAILED, {"summary": response}, 0.0, [response])
            
        return AgentResult(task.task_id, self.name, TaskStatus.DONE, {
            "summary": "Completed via Qwen Code CLI", 
            "raw_output": response,
            "model_used": selected_model,
            "tokens_estimated": usage
        }, 0.95)


    def before_task(self, task: Task, context: dict[str, Any]) -> None:
        # Auto-inject Qwen as a preferred provider for certain tasks
        if task.type in {TaskType.CODE, TaskType.TEST} and "qwen" not in context.get("preferred_providers", []):
            context.setdefault("preferred_providers", []).append("qwen")

    def after_task(self, task: Task, result: AgentResult, context: dict[str, Any]) -> None:
        pass

    def finalize(self) -> dict[str, Any]:
        return {
            "status": "active" if self._binary_path else "missing",
            "version": self._get_version(),
            "binary": self._binary_path,
            "model": self.config.default_model
        }
