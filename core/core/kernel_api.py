from __future__ import annotations

from dataclasses import dataclass

from .kernel_protocol import KernelAPI, ModuleStateMap


@dataclass(slots=True)
class KernelCommunicationLayer:
    """Gateway managing communications between the orchestrator and kernel modules."""

    _api_provider: KernelAPI

    def dispatch(self, module_name: str, event_name: str, payload: ModuleStateMap) -> None:
        event_payload: ModuleStateMap = {"_from": module_name, **payload}
        self._api_provider.emit_event(event_name, event_payload)
        self._api_provider.log("info", f"[COMM] Module {module_name} emitted {event_name}")
