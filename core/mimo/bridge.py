from __future__ import annotations

from dataclasses import dataclass
import asyncio
import json
import logging
from typing import Optional

logger = logging.getLogger(__name__)


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
        self.is_cli_alive = True

    async def get_models(self) -> list[MimoModelSnapshot]:
        try:
            proc = await asyncio.create_subprocess_exec(
                "mimo", "models", "--verbose",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await proc.communicate()
            if proc.returncode != 0:
                self.is_cli_alive = False
                logger.critical("MIMO CLI failed: %s", stderr.decode("utf-8", errors="ignore").strip())
                return []
            models = self._parse_models_output(stdout.decode("utf-8", errors="ignore"))
            self._cached_models = models
            self.is_cli_alive = True
            return models
        except FileNotFoundError:
            self.is_cli_alive = False
            logger.critical("MIMO CLI not found in PATH")
            return []
        except Exception as exc:
            self.is_cli_alive = False
            logger.critical("Unexpected MIMO bridge failure: %s", exc)
            return []

    def get_cached_models(self) -> list[MimoModelSnapshot]:
        return list(self._cached_models)

    async def ping_model(self, model_name: str) -> bool:
        normalized = (model_name or "").strip().lower()
        if not normalized:
            return False

        # Current mimo CLI does not expose a dedicated `ping` subcommand.
        # Treat model discovery as the compatibility health-check so the
        # director can stay operational with the installed CLI.
        cached = self.get_cached_models()
        if not cached:
            cached = await self.refresh_cache()
        for model in cached:
            full_id = (model.full_id or "").strip().lower()
            short_id = (model.id or "").strip().lower()
            if normalized in {full_id, short_id}:
                return str(model.status or "").lower() not in {"offline", "error", "disabled"}
        return False

    async def refresh_cache(self) -> list[MimoModelSnapshot]:
        return await self.get_models()

    def _parse_models_output(self, output: str) -> list[MimoModelSnapshot]:
        if not output.strip():
            return []
        results: list[MimoModelSnapshot] = []
        lines = output.strip().splitlines()
        i = 0
        while i < len(lines):
            full_id = lines[i].strip()
            i += 1
            if not full_id:
                continue
            payload = []
            depth = 0
            started = False
            while i < len(lines):
                line = lines[i]
                payload.append(line)
                depth += line.count("{")
                depth -= line.count("}")
                started = started or "{" in line
                i += 1
                if started and depth <= 0:
                    break
            try:
                data = json.loads("\n".join(payload))
            except json.JSONDecodeError:
                continue
            capability_tags = data.get("capabilities") or data.get("capabilityTags") or []
            if not isinstance(capability_tags, list):
                capability_tags = []
            results.append(MimoModelSnapshot(
                full_id=full_id,
                id=str(data.get("id", "")),
                provider=str(data.get("providerID", "")),
                status=str(data.get("status", "")),
                context_window=(data.get("limit") or {}).get("context"),
                capability_tags=[str(item).strip() for item in capability_tags if str(item).strip()],
                cost_class=str(data.get("costClass") or data.get("cost_class") or "").strip() or None,
                ready=bool(data.get("ready")) if "ready" in data else None,
                blocked=bool(data.get("blocked", False)),
            ))
        return results


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
            models = await self.bridge.refresh_cache()
            for model in models:
                ok = await self.bridge.ping_model(model.id or model.full_id)
                if not ok:
                    model.status = "OFFLINE"
            await asyncio.sleep(self.interval_sec)
