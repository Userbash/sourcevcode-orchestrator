from __future__ import annotations

from typing import Any, Protocol


class OrchestrationBackend(Protocol):
    async def run(self, payload: dict[str, Any]) -> dict[str, Any]: ...
