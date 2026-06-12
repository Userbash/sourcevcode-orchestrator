from dataclasses import dataclass
from datetime import datetime
from typing import Optional, Any
from enum import Enum

class HealthStatus(Enum):
    READY = "ready"
    BUSY = "busy"
    DEGRADED = "degraded"
    OFFLINE = "offline"
    OVERLOADED = "overloaded"
    UNREACHABLE = "unreachable"
    FAILED = "failed"
    DISABLED = "disabled"
    QUOTA_EMPTY = "quota_empty"
    AUTH_FAILED = "auth_failed"

class Readiness(Enum):
    COLD = "cold"
    WARM = "warm"
    HOT = "hot"

@dataclass
class HealthcheckResult:
    agent_id: str
    status: HealthStatus
    readiness: Readiness
    capabilities: list[str]
    active_tasks: int
    queue_depth: int
    avg_latency_ms: float
    success_rate: float
    error_rate: float
    quota_remaining: int
    timestamp: datetime = field(default_factory=datetime.utcnow)
    last_error: Optional[str] = None

class ExternalAIModuleHealthchecker:
    def check(self, module: Any) -> HealthcheckResult:
        """Perform a standard readiness probe against the agent bridge."""
        raise NotImplementedError
    
    def fallback_ping(self, module: Any) -> HealthcheckResult:
        """Perform a fallback ping if a direct readiness probe is unavailable."""
        raise NotImplementedError

    def is_available(self, module: Any, task: Any) -> bool:
        """Check if the module is available for a specific task."""
        raise NotImplementedError
