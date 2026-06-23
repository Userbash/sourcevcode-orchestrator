from __future__ import annotations

from dataclasses import dataclass
import asyncio
from typing import Optional

from core.core.mimo_provider import configured_native_mimo_models, normalize_mimo_model_name


@dataclass(slots=True)
class MimoModelSnapshot:
    full_id: str
    id: str
    provider: str
    status: str
    context_window: Optional[int]
    capability_tags: list[str] | None = None
    cost_class: str | None = None
    ready: bool | None = None
    blocked: bool = False


class MimoAsyncBridge:
    def __init__(self) -> None:
        self._cached_models: list[MimoModelSnapshot] = []
        self.is_catalog_available = False

    def _catalog(self) -> list[MimoModelSnapshot]:
        return [
            MimoModelSnapshot(
                full_id=model,
                id=normalize_mimo_model_name(model),
                provider='mimo',
                status='ONLINE',
                context_window=None,
                capability_tags=['code', 'review', 'plan', 'test', 'docs', 'research'],
                cost_class='remote',
                ready=True,
                blocked=False,
            )
            for model in configured_native_mimo_models()
        ]

    async def get_models(self) -> list[MimoModelSnapshot]:
        self._cached_models = self._catalog()
        self.is_catalog_available = bool(self._cached_models)
        return list(self._cached_models)

    def get_models_sync(self) -> list[MimoModelSnapshot]:
        self._cached_models = self._catalog()
        self.is_catalog_available = bool(self._cached_models)
        return list(self._cached_models)

    def get_cached_models(self) -> list[MimoModelSnapshot]:
        return list(self._cached_models)

    async def ping_model(self, model_name: str) -> bool:
        normalized = str(model_name or '').strip().lower()
        if not normalized:
            return False
        cached = self.get_cached_models() or await self.refresh_cache()
        return any(normalized in {str(item.full_id).lower(), str(item.id).lower()} for item in cached)

    async def refresh_cache(self) -> list[MimoModelSnapshot]:
        return await self.get_models()

    def refresh_cache_sync(self) -> list[MimoModelSnapshot]:
        return self.get_models_sync()

    def _parse_models_output(self, output: str) -> list[MimoModelSnapshot]:
        return self._catalog()


class MimoHealthChecker:
    def __init__(self, bridge: MimoAsyncBridge, interval_sec: float = 300.0) -> None:
        self.bridge = bridge
        self.interval_sec = interval_sec
        self._task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    async def _run(self) -> None:
        while True:
            await self.bridge.refresh_cache()
            await asyncio.sleep(self.interval_sec)
