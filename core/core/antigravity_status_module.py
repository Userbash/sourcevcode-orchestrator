from __future__ import annotations

import os
import threading
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from .kernel_protocol import KernelAPI


_shared_status_module: "AntigravityStatusModule | None" = None
_shared_status_lock = threading.RLock()


def shared_antigravity_snapshot(*, force: bool = False) -> dict[str, Any]:
    global _shared_status_module
    with _shared_status_lock:
        if _shared_status_module is None:
            _shared_status_module = AntigravityStatusModule()
        module = _shared_status_module
    return module.refresh(force=force) if force else module.snapshot()


@dataclass(slots=True)
class AntigravityStatusModule:
    name: str = "antigravity_status"
    _api: KernelAPI | None = None
    _last_status: dict[str, Any] = field(default_factory=dict)
    _last_refresh_at: str | None = None
    _last_error: str | None = None
    _poll_thread: threading.Thread | None = None
    _stop_event: threading.Event = field(default_factory=threading.Event)
    _lock: threading.RLock = field(default_factory=threading.RLock)

    @staticmethod
    def _ttl_sec() -> int:
        raw = os.getenv("AI_BRIDGE_ANTIGRAVITY_STATUS_TTL_SEC", os.getenv("AI_BRIDGE_ANTIGRAVITY_POLL_INTERVAL_SEC", "60")).strip()
        try:
            return max(10, int(raw))
        except ValueError:
            return 60

    @staticmethod
    def _poll_interval_sec() -> int:
        raw = os.getenv("AI_BRIDGE_ANTIGRAVITY_POLL_INTERVAL_SEC", "45").strip()
        try:
            return max(15, int(raw))
        except ValueError:
            return 45

    def on_load(self, api: KernelAPI) -> None:
        self._api = api
        self.refresh(force=True)
        if self._poll_thread is None or not self._poll_thread.is_alive():
            self._stop_event.clear()
            self._poll_thread = threading.Thread(target=self._poll_loop, name="antigravity-status-poll", daemon=True)
            self._poll_thread.start()

    def on_unload(self) -> None:
        self._stop_event.set()

    def _manager(self):
        try:
            from .integrations.antigravity_manager import AntigravityManager
            return AntigravityManager()
        except Exception as exc:
            self._last_error = str(exc)
            return None

    def _make_status(self, health: dict[str, Any], *, retry: dict[str, Any] | None = None) -> dict[str, Any]:
        ready = bool(health.get("ready"))
        status = {
            "ok": ready,
            "ready": ready,
            "status": "ready" if ready else "degraded",
            "auth_mode": health.get("auth_mode", "agy_oauth"),
            "models": health.get("models", []),
            "models_probe": health.get("models_probe", {}),
            "generation_probe": health.get("generation_probe", {}),
            "auth_probe": health.get("auth_probe", {}),
            "api_probe": health.get("api_probe", {}),
            "error": None if ready else (health.get("models_probe", {}) or {}).get("stderr") or (health.get("generation_probe", {}) or {}).get("stderr") or (health.get("auth_probe", {}) or {}).get("stderr"),
            "updated_at": datetime.now(UTC).isoformat(),
        }
        if retry is not None:
            status["auth_retry"] = retry
            if retry.get("ok"):
                status["ok"] = True
        return status

    def refresh(self, *, force: bool = False) -> dict[str, Any]:
        manager = self._manager()
        if manager is None:
            status = {
                "ok": False,
                "ready": False,
                "status": "error",
                "error": self._last_error or "manager_unavailable",
                "updated_at": datetime.now(UTC).isoformat(),
            }
            with self._lock:
                self._last_status = status
                self._last_refresh_at = status["updated_at"]
            return status

        try:
            health = manager.status()
            retry = None
            if force and not health.get("ready"):
                retry = manager.ensure_authorized()
                if retry.get("ok"):
                    health = manager.status()
            status = self._make_status(health, retry=retry)
        except Exception as exc:
            self._last_error = str(exc)
            status = {
                "ok": False,
                "ready": False,
                "status": "error",
                "error": str(exc),
                "updated_at": datetime.now(UTC).isoformat(),
            }
        with self._lock:
            self._last_status = status
            self._last_refresh_at = status["updated_at"]
        return status

    def _is_stale(self) -> bool:
        if not self._last_refresh_at:
            return True
        try:
            refreshed = datetime.fromisoformat(self._last_refresh_at)
            age = (datetime.now(UTC) - refreshed).total_seconds()
            return age >= self._ttl_sec()
        except Exception:
            return True

    def _poll_loop(self) -> None:
        interval = self._poll_interval_sec()
        while not self._stop_event.wait(interval):
            try:
                self.refresh(force=False)
            except Exception as exc:
                self._last_error = str(exc)

    def reconcile(self) -> dict[str, Any]:
        return self.refresh(force=True)

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            stale = self._is_stale()
        if stale:
            self.refresh(force=False)
        with self._lock:
            if not self._last_status:
                self.refresh(force=False)
            return {**self._last_status, "last_refresh_at": self._last_refresh_at, "last_error": self._last_error, "ttl_sec": self._ttl_sec(), "poll_interval_sec": self._poll_interval_sec()}

    def before_task(self, task: Any, context: dict[str, Any]) -> None:
        if context.get("needs_antigravity_status"):
            context["antigravity_status"] = self.snapshot()

    def state(self) -> dict[str, Any]:
        with self._lock:
            return {
                "snapshot": {**self._last_status},
                "last_refresh_at": self._last_refresh_at,
                "last_error": self._last_error,
                "ttl_sec": self._ttl_sec(),
                "poll_interval_sec": self._poll_interval_sec(),
            }

    def finalize(self) -> dict[str, Any]:
        return self.state()
