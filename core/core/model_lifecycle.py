from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from enum import Enum
from typing import Dict

logger = logging.getLogger("model_lifecycle")

class ModelHealth(Enum):
    READY = "ready"
    COOLDOWN = "cooldown"     # Temporary block (e.g. rate limit)
    EXHAUSTED = "exhausted"   # Context or token limit hit
    CRITICAL_FAIL = "fail"    # Model/Provider is down
    ZOMBIE = "zombie"         # Network/Service unreachable

@dataclass
class ModelState:
    model_name: str
    status: ModelHealth = ModelHealth.READY
    last_error: str | None = None
    retry_at: float = 0
    context_used: int = 0
    total_tokens_consumed: int = 0

class ErrorClassifier:
    """Classifies errors to determine how to handle model exclusion."""
    
    @staticmethod
    def is_fatal(error_msg: str) -> bool:
        fatal_keywords = [
            "insufficient_quota", "account_deactivated", "invalid_api_key", 
            "payment_required", "credit_balance_too_low", "billing_hard_limit"
        ]
        return any(k in error_msg.lower() for k in fatal_keywords)

    @staticmethod
    def is_rate_limit(error_msg: str) -> bool:
        return "429" in error_msg or "rate_limit" in error_msg.lower() or "too many requests" in error_msg.lower()

    @staticmethod
    def is_context_limit(error_msg: str) -> bool:
        return any(k in error_msg.lower() for k in ["context_length", "maximum context length", "token limit exceeded"])

    @staticmethod
    def is_network_fail(error_msg: str) -> bool:
        return any(k in error_msg.lower() for k in ["connection refused", "503", "502", "unreachable", "timeout"])

class ModelLifecycleManager:
    """
    Manages the health and "soft unloading" of AI models within the kernel.
    Ensures the orchestrator avoids broken or overloaded models.
    """
    def __init__(self) -> None:
        self._states: Dict[str, ModelState] = {}
        self.default_cooldown = 300  # 5 minutes
        self.classifier = ErrorClassifier()

    def report_failure(self, model_name: str, error: str) -> None:
        state = self._get_or_create(model_name)
        state.last_error = error
        
        if self.classifier.is_fatal(error):
            state.status = ModelHealth.CRITICAL_FAIL
            state.retry_at = time.time() + 86400  # Block for 24 hours
            logger.critical(f"[LIFECYCLE] Model {model_name} had FATAL failure (Auth/Quota). Hard exclusion active.")
            
        elif self.classifier.is_context_limit(error):
            state.status = ModelHealth.EXHAUSTED
            state.retry_at = time.time() + 3600  # Block for 1 hour
            logger.warning(f"[LIFECYCLE] Model {model_name} exhausted context/tokens. Soft unloading.")
            
        elif self.classifier.is_network_fail(error):
            state.status = ModelHealth.ZOMBIE
            state.retry_at = time.time() + 600  # Block for 10 minutes, then re-probe
            logger.error(f"[LIFECYCLE] Model {model_name} is a ZOMBIE (Network/Service down).")
            
        elif self.classifier.is_rate_limit(error):
            state.status = ModelHealth.COOLDOWN
            state.retry_at = time.time() + self.default_cooldown
            logger.warning(f"[LIFECYCLE] Model {model_name} entered Rate Limit COOLDOWN.")
        else:
            # Unknown error, temporary cooldown
            state.status = ModelHealth.COOLDOWN
            state.retry_at = time.time() + 60
            logger.error(f"[LIFECYCLE] Model {model_name} failed with unknown error: {error}")

    def is_available(self, model_name: str) -> bool:
        state = self._states.get(model_name)
        if not state:
            return True
        if state.status == ModelHealth.READY:
            return True
            
        if time.time() > state.retry_at:
            state.status = ModelHealth.READY
            logger.info(f"[LIFECYCLE] Model {model_name} recovered and is back to READY.")
            return True
            
        return False

    def get_status(self, model_name: str) -> str:
        state = self._states.get(model_name)
        return state.status.value if state else "ready"

    def _get_or_create(self, model_name: str) -> ModelState:
        if model_name not in self._states:
            self._states[model_name] = ModelState(model_name=model_name)
        return self._states[model_name]
