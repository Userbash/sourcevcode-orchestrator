from __future__ import annotations

import asyncio
import inspect
import threading
from collections.abc import Coroutine
from dataclasses import dataclass, field

from .kernel_protocol import KernelAPI, KernelModule, ModuleStateMap, TaskContextMap
from .models import AgentResult, Task


@dataclass(slots=True)
class KernelModuleManager:
    _modules: dict[str, KernelModule] = field(default_factory=dict)
    _loaded: set[str] = field(default_factory=set)
    _api: KernelAPI | None = None

    def set_api(self, api: KernelAPI) -> None:
        self._api = api

    def register(self, module: KernelModule) -> None:
        self._modules[module.name] = module

    @staticmethod
    def _run_coroutine_blocking(coro: Coroutine[object, object, None]) -> None:
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            asyncio.run(coro)
            return

        result: dict[str, BaseException | None] = {"error": None}

        def runner() -> None:
            try:
                asyncio.run(coro)
            except BaseException as exc:  # pragma: no cover - propagated to caller
                result["error"] = exc

        thread = threading.Thread(target=runner, daemon=True)
        thread.start()
        thread.join()
        if result["error"] is not None:
            raise result["error"]

    def load(self, name: str) -> None:
        if self._api is None:
            raise RuntimeError("KernelAPI not initialized in ModuleManager")
        module = self._modules.get(name)
        if not module or name in self._loaded:
            return

        try:
            if inspect.iscoroutinefunction(module.on_load):
                self._run_coroutine_blocking(module.on_load(self._api))
            else:
                module.on_load(self._api)
            self._loaded.add(name)
        except Exception as exc:
            if self._api:
                self._api.log("error", f"[KERNEL] Module {name} failed to load: {exc}")
            raise

    def unload(self, name: str) -> None:
        if name not in self._loaded:
            return
        module = self._modules.get(name)
        if module is None:
            self._loaded.remove(name)
            return
        try:
            if hasattr(module, "on_unload"):
                if inspect.iscoroutinefunction(module.on_unload):
                    self._run_coroutine_blocking(module.on_unload())
                else:
                    module.on_unload()
        except Exception as exc:
            if self._api:
                self._api.log("error", f"[KERNEL] Module {name} failed to unload: {exc}")
            raise
        finally:
            self._loaded.remove(name)

    def get_module(self, name: str) -> KernelModule | None:
        return self._modules.get(name)

    def is_loaded(self, name: str) -> bool:
        return name in self._loaded

    def loaded_modules(self) -> list[str]:
        return sorted(self._loaded)

    def before_task(self, task: Task, context: TaskContextMap) -> None:
        for name in self.loaded_modules():
            module = self._modules[name]
            if hasattr(module, "before_task"):
                module.before_task(task, context)

    def after_task(self, task: Task, result: AgentResult, context: TaskContextMap) -> None:
        for name in self.loaded_modules():
            module = self._modules[name]
            if hasattr(module, "after_task"):
                module.after_task(task, result, context)

    def finalize(self) -> dict[str, ModuleStateMap]:
        data: dict[str, ModuleStateMap] = {}
        for name in self.loaded_modules():
            module = self._modules[name]
            if hasattr(module, "finalize"):
                data[name] = module.finalize()
        return data
