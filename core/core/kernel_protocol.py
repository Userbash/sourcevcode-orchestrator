from __future__ import annotations

from collections.abc import Awaitable, Mapping
from typing import Protocol, TypeAlias, runtime_checkable

TaskContextMap: TypeAlias = dict[str, object]
ModuleStateMap: TypeAlias = dict[str, object]
MaybeAwaitableNone: TypeAlias = None | Awaitable[None]


@runtime_checkable
class KernelAPI(Protocol):
    """Canonical internal API exposed by the orchestrator to kernel modules."""

    def get_context(self, key: str) -> object | None:
        ...

    def emit_event(self, event_name: str, payload: Mapping[str, object]) -> None:
        ...

    def query_state(self, module_name: str, key: str) -> object | None:
        ...

    def query_module_state(self, module_name: str, key: str) -> object | None:
        ...

    def log(self, level: str, message: str) -> None:
        ...

    def get_module(self, name: str) -> object | None:
        ...

    def get_memory(self) -> object:
        ...

    def load_module(self, name: str) -> None:
        ...

    def unload_module(self, name: str) -> None:
        ...


class KernelModule(Protocol):
    """Protocol implemented by loadable orchestrator kernel modules."""

    name: str

    def on_load(self, api: KernelAPI) -> MaybeAwaitableNone:
        ...

    def on_unload(self) -> MaybeAwaitableNone:
        ...

    def before_task(self, task: object, context: TaskContextMap) -> None:
        ...

    def after_task(self, task: object, result: object, context: TaskContextMap) -> None:
        ...

    def finalize(self) -> ModuleStateMap:
        ...
