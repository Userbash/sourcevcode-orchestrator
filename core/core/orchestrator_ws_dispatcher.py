from __future__ import annotations

import asyncio
import inspect
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Any

from .ws_protocol import WsEnvelope, build_ack, build_error, build_event, build_response

JsonDict = dict[str, Any]
SingleResponseResult = tuple[JsonDict, int]
SingleHandler = Callable[[WsEnvelope], SingleResponseResult | Awaitable[SingleResponseResult] | JsonDict | Awaitable[JsonDict]]
StreamHandler = Callable[[WsEnvelope], AsyncIterator[JsonDict] | Awaitable[AsyncIterator[JsonDict]]]
Handler = Callable[[WsEnvelope], Any]

ERROR_UNSUPPORTED_ACTION = "UNSUPPORTED_ACTION"
ERROR_TIMEOUT = "TIMEOUT"
ERROR_INTERNAL = "INTERNAL_ERROR"


@dataclass(slots=True)
class HandlerRegistration:
    handler: Handler
    mode: str = "single"
    send_ack: bool = True


class OrchestratorWsDispatcher:
    def __init__(self) -> None:
        self._handlers: dict[str, HandlerRegistration] = {}

    def register(self, action: str, handler: Handler, *, mode: str = "single", send_ack: bool = True) -> None:
        self._handlers[_normalize_action(action)] = HandlerRegistration(handler=handler, mode=mode, send_ack=send_ack)

    def register_single(self, action: str, handler: SingleHandler, *, send_ack: bool = False) -> None:
        self.register(action, handler, mode="single", send_ack=send_ack)

    def register_stream(self, action: str, handler: StreamHandler, *, send_ack: bool = True) -> None:
        self.register(action, handler, mode="stream", send_ack=send_ack)

    def registration(self, action: str) -> HandlerRegistration | None:
        return self._handlers.get(_normalize_action(action))

    def supports(self, action: str) -> bool:
        return self.registration(action) is not None

    def has_action(self, action: str) -> bool:
        return self.supports(action)

    def actions(self) -> list[str]:
        return sorted(self._handlers.keys())

    async def dispatch(self, request: WsEnvelope | Mapping[str, Any]) -> AsyncIterator[JsonDict]:
        envelope = request if isinstance(request, WsEnvelope) else WsEnvelope.from_dict(request)
        registration = self.registration(envelope.action or "")
        if registration is None:
            yield build_error(
                envelope.request_id,
                ERROR_UNSUPPORTED_ACTION,
                action=envelope.action,
                correlation_id=envelope.correlation_id,
                message=f"unsupported action: {envelope.action}",
            )
            return

        if envelope.ack and registration.send_ack:
            yield build_ack(
                envelope.request_id,
                correlation_id=envelope.correlation_id,
                action=envelope.action,
                data={
                    "accepted": True,
                    "mode": registration.mode,
                    "idempotency_key": envelope.idempotency_key,
                    "timeout_ms": envelope.timeout_ms,
                },
            )

        try:
            iterator = self._apply_timeout(self._invoke_handler(registration, envelope), envelope.timeout_ms)
            async for payload in iterator:
                yield payload
        except asyncio.TimeoutError:
            yield build_error(
                envelope.request_id,
                ERROR_TIMEOUT,
                action=envelope.action,
                correlation_id=envelope.correlation_id,
                message="request timed out",
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            yield build_error(
                envelope.request_id,
                ERROR_INTERNAL,
                action=envelope.action,
                correlation_id=envelope.correlation_id,
                message=str(exc),
                details={"handler": envelope.action or ""},
            )

    async def collect_response(self, request: WsEnvelope | Mapping[str, Any]) -> JsonDict:
        last_payload: JsonDict | None = None
        async for payload in self.dispatch(request):
            last_payload = payload
            if bool(payload.get("final")):
                return payload
        return last_payload or {}

    async def execute_http(self, request: WsEnvelope | Mapping[str, Any]) -> tuple[JsonDict, int]:
        envelope = request if isinstance(request, WsEnvelope) else WsEnvelope.from_dict(request)
        registration = self.registration(envelope.action or "")
        if registration is None:
            return {
                "status": "error",
                "error": ERROR_UNSUPPORTED_ACTION,
                "message": f"unsupported action: {envelope.action}",
            }, 404

        if registration.mode != "single":
            return {
                "status": "error",
                "error": "stream_only_action",
                "message": f"action {envelope.action} requires websocket streaming",
            }, 400

        result = await self._call(registration.handler, envelope)
        if isinstance(result, tuple) and len(result) == 2:
            payload, status_code = result
            return dict(payload), int(status_code)
        if isinstance(result, Mapping):
            return dict(result), 200
        return {"status": "ok", "data": result}, 200

    async def _invoke_handler(self, registration: HandlerRegistration, request: WsEnvelope) -> AsyncIterator[JsonDict]:
        result = await self._call(registration.handler, request)
        if registration.mode == "stream":
            async for item in self._iterate_result(request, result, final_default=False):
                yield item
            return
        async for item in self._iterate_result(request, result, final_default=True):
            yield item

    async def _call(self, handler: Handler, request: WsEnvelope) -> Any:
        result = handler(request)
        if inspect.isawaitable(result):
            return await result
        return result

    async def _iterate_result(self, request: WsEnvelope, result: Any, *, final_default: bool) -> AsyncIterator[JsonDict]:
        if inspect.isasyncgen(result) or hasattr(result, "__aiter__"):
            saw_item = False
            async for item in result:
                saw_item = True
                yield self._normalize_payload(request, item, final_default=False)
            if not saw_item and not final_default:
                yield build_response(
                    request.request_id,
                    action=request.action,
                    correlation_id=request.correlation_id,
                    data={},
                    final=True,
                )
            return
        yield self._normalize_payload(request, result, final_default=final_default)

    def _normalize_payload(self, request: WsEnvelope, payload: Any, *, final_default: bool) -> JsonDict:
        if isinstance(payload, Mapping) and {"type", "request_id", "action", "final"} <= payload.keys():
            return dict(payload)
        if isinstance(payload, tuple) and len(payload) == 2:
            body, _status = payload
            return build_response(
                request.request_id,
                action=request.action,
                correlation_id=request.correlation_id,
                data=dict(body) if isinstance(body, Mapping) else {"value": body},
                final=final_default,
            )
        if isinstance(payload, Mapping):
            normalized = dict(payload)
            frame_type = str(normalized.get("type") or ("response" if final_default else "event"))
            final = bool(normalized.get("final", final_default))
            if frame_type == "error":
                error = normalized.get("error")
                if isinstance(error, Mapping):
                    return build_error(
                        request.request_id,
                        str(error.get("code") or ERROR_INTERNAL),
                        action=request.action,
                        correlation_id=request.correlation_id,
                        message=str(error.get("message") or normalized.get("message") or "request failed"),
                        details=dict(error.get("details") or {}) if isinstance(error.get("details"), Mapping) else None,
                        final=final,
                    )
                return build_error(
                    request.request_id,
                    str(normalized.get("error") or ERROR_INTERNAL),
                    action=request.action,
                    correlation_id=request.correlation_id,
                    message=str(normalized.get("message") or normalized.get("error") or "request failed"),
                    final=final,
                )
            if frame_type in {"event", "snapshot", "delta"}:
                return build_event(
                    request.request_id,
                    action=request.action or "",
                    correlation_id=request.correlation_id,
                    data=normalized,
                    final=final,
                    frame_type=frame_type,
                )
            return build_response(
                request.request_id,
                action=request.action,
                correlation_id=request.correlation_id,
                data=normalized,
                final=final,
            )
        return build_response(
            request.request_id,
            action=request.action,
            correlation_id=request.correlation_id,
            data={"value": payload},
            final=final_default,
        )

    async def _apply_timeout(self, iterator: AsyncIterator[JsonDict], timeout_ms: int | None) -> AsyncIterator[JsonDict]:
        if not timeout_ms:
            async for item in iterator:
                yield item
            return
        while True:
            try:
                item = await asyncio.wait_for(iterator.__anext__(), timeout=timeout_ms / 1000)
            except StopAsyncIteration:
                return
            yield item


OrchestratorWSDispatcher = OrchestratorWsDispatcher
OrchestratorTransportRequest = WsEnvelope


def build_orchestrator_ws_dispatcher(orchestrator: Any) -> OrchestratorWsDispatcher:
    from .orchestrator_transport import (
        ai_kernel_ensure_payload,
        ai_kernel_gate_payload,
        diagnostics_payload,
        local_llm_connect_payload,
        local_llm_disconnect_payload,
        local_llm_residents_payload,
        local_llm_warm_payload,
        provider_inventory_payload,
        provider_inventory_single_payload,
        provider_inventory_stream,
        provider_model_lookup_payload,
        provider_models_index_payload,
        provider_models_index_stream,
        provider_runtime_inventory_all_payload,
        provider_runtime_inventory_single_payload,
        provider_runtime_inventory_stream,
        runtime_events_stream,
        socraticode_context_compaction_status_payload,
        socraticode_context_compaction_status_stream,
        transport_audit_payload,
    )

    dispatcher = OrchestratorWsDispatcher()

    dispatcher.register_single(
        "providers.inventory.get",
        lambda request: provider_inventory_payload(orchestrator, force_refresh=_bool_field(request, "force_refresh")),
    )
    dispatcher.register_single(
        "providers.inventory.provider.get",
        lambda request: provider_inventory_single_payload(
            orchestrator,
            str(request.data.get("provider") or ""),
            force_refresh=_bool_field(request, "force_refresh"),
        ),
    )
    dispatcher.register_single(
        "providers.runtime_inventory.get",
        lambda request: provider_runtime_inventory_all_payload(
            orchestrator,
            force_refresh=_bool_field(request, "force_refresh"),
            probe_limit=_int_field(request, "probe_limit"),
        ),
    )
    dispatcher.register_single(
        "providers.runtime_inventory.provider.get",
        lambda request: provider_runtime_inventory_single_payload(
            orchestrator,
            str(request.data.get("provider") or ""),
            force_refresh=_bool_field(request, "force_refresh"),
            probe_limit=_int_field(request, "probe_limit"),
        ),
    )
    dispatcher.register_single(
        "providers.models.index.get",
        lambda request: provider_models_index_payload(orchestrator, force_refresh=_bool_field(request, "force_refresh")),
    )
    dispatcher.register_single(
        "providers.models.lookup.get",
        lambda request: provider_model_lookup_payload(
            orchestrator,
            str(request.data.get("model_name") or ""),
            force_refresh=_bool_field(request, "force_refresh"),
        ),
    )
    dispatcher.register_stream("providers.inventory.subscribe", lambda request: provider_inventory_stream(orchestrator))
    dispatcher.register_stream("providers.runtime_inventory.subscribe", lambda request: provider_runtime_inventory_stream(orchestrator))
    dispatcher.register_stream("providers.models.index.subscribe", lambda request: provider_models_index_stream(orchestrator))
    dispatcher.register_single("providers.local_llm.residents.get", lambda request: local_llm_residents_payload(orchestrator))
    dispatcher.register_single("providers.local_llm.connect", lambda request: local_llm_connect_payload(orchestrator, dict(request.data)), send_ack=True)
    dispatcher.register_single("providers.local_llm.disconnect", lambda request: local_llm_disconnect_payload(orchestrator, dict(request.data)), send_ack=True)
    dispatcher.register_single("providers.local_llm.warm", lambda request: local_llm_warm_payload(orchestrator, dict(request.data)), send_ack=True)
    dispatcher.register_single(
        "providers.ai_kernel.gate.get",
        lambda request: ai_kernel_gate_payload(
            orchestrator,
            ensure_ready=_bool_field(request, "ensure_ready"),
            model_name=str(request.data.get("model_name") or "").strip() or None,
        ),
    )
    dispatcher.register_single("providers.ai_kernel.ensure", lambda request: ai_kernel_ensure_payload(orchestrator, dict(request.data)), send_ack=True)
    dispatcher.register_single("transport.audit.get", lambda request: transport_audit_payload(orchestrator))
    dispatcher.register_single("socraticode.context_compaction.status.get", lambda request: socraticode_context_compaction_status_payload(orchestrator))
    dispatcher.register_single(
        "diagnostics.get",
        lambda request: diagnostics_payload(
            orchestrator,
            layers=_layers_field(request, "layers"),
            matrix_only=_bool_field(request, "matrix_only"),
        ),
    )
    dispatcher.register_stream("runtime.events.subscribe", lambda request: runtime_events_stream(orchestrator))
    dispatcher.register_stream("socraticode.context_compaction.status.subscribe", lambda request: socraticode_context_compaction_status_stream(orchestrator))
    return dispatcher


def _normalize_action(action: str) -> str:
    return str(action or "").strip().lower()


def _bool_field(request: WsEnvelope, field_name: str) -> bool:
    value = request.data.get(field_name)
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return bool(value)


def _int_field(request: WsEnvelope, field_name: str) -> int | None:
    value = request.data.get(field_name)
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _layers_field(request: WsEnvelope, field_name: str) -> list[str] | None:
    value = request.data.get(field_name)
    if isinstance(value, list):
        return [str(item) for item in value if str(item).strip()]
    if isinstance(value, str) and value.strip():
        return [item.strip() for item in value.split(",") if item.strip()]
    return None
