from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping
from uuid import uuid4


JsonDict = dict[str, Any]
RequestIdFactory = Callable[[], str]

REQUEST_FRAME_TYPES = frozenset({"command", "subscribe", "unsubscribe", "cancel"})
SERVER_FRAME_TYPES = frozenset({"ack", "event", "response", "error", "snapshot", "delta", "heartbeat", "pong"})
FRAME_TYPES = REQUEST_FRAME_TYPES | SERVER_FRAME_TYPES

ERROR_TAXONOMY: dict[str, JsonDict] = {
    "INVALID_JSON": {
        "retryable": False,
        "category": "protocol",
        "message": "Frame payload is not valid JSON.",
    },
    "INVALID_FRAME": {
        "retryable": False,
        "category": "protocol",
        "message": "Frame payload must be a JSON object.",
    },
    "INVALID_TYPE": {
        "retryable": False,
        "category": "protocol",
        "message": "Frame type is not supported by the WS protocol contract.",
    },
    "INVALID_ACTION": {
        "retryable": False,
        "category": "protocol",
        "message": "Frame action is missing or invalid for this frame type.",
    },
    "INVALID_REQUEST_ID": {
        "retryable": False,
        "category": "protocol",
        "message": "Frame request_id is missing or invalid.",
    },
    "INVALID_TIMEOUT": {
        "retryable": False,
        "category": "protocol",
        "message": "Frame timeout_ms must be a positive integer.",
    },
    "INVALID_IDEMPOTENCY_KEY": {
        "retryable": False,
        "category": "protocol",
        "message": "Frame idempotency_key must be a non-empty string when provided.",
    },
    "BAD_REQUEST": {
        "retryable": False,
        "category": "client",
        "message": "The request payload is semantically invalid.",
    },
    "UNAUTHORIZED": {
        "retryable": False,
        "category": "auth",
        "message": "Authentication is required or invalid.",
    },
    "FORBIDDEN": {
        "retryable": False,
        "category": "auth",
        "message": "The caller is not allowed to perform this action.",
    },
    "NOT_FOUND": {
        "retryable": False,
        "category": "routing",
        "message": "The requested action or resource was not found.",
    },
    "CONFLICT": {
        "retryable": False,
        "category": "mutation",
        "message": "The request conflicts with current orchestrator state.",
    },
    "UNSUPPORTED_ACTION": {
        "retryable": False,
        "category": "routing",
        "message": "The requested action is not registered in the dispatcher.",
    },
    "TIMEOUT": {
        "retryable": True,
        "category": "mutation",
        "message": "The request timed out before the terminal event arrived.",
    },
    "CANCELED": {
        "retryable": True,
        "category": "mutation",
        "message": "The request was canceled before completion.",
    },
    "RATE_LIMITED": {
        "retryable": True,
        "category": "provider",
        "message": "The request was rejected due to rate limiting.",
    },
    "UNAVAILABLE": {
        "retryable": True,
        "category": "provider",
        "message": "The requested service is temporarily unavailable.",
    },
    "INTERNAL_ERROR": {
        "retryable": True,
        "category": "server",
        "message": "The orchestrator failed while handling the request.",
    },
}


def _new_request_id() -> str:
    return uuid4().hex


def _copy_json_dict(value: Mapping[str, Any] | None) -> JsonDict:
    return dict(value or {})


def _normalize_optional_str(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _require_non_empty_str(value: Any, *, code: str, field_name: str) -> str:
    text = _normalize_optional_str(value)
    if text is None:
        raise WsProtocolValidationError(f"{field_name} must be a non-empty string", code=code)
    return text


def _normalize_timeout_ms(value: Any) -> int | None:
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        raise WsProtocolValidationError("timeout_ms must be a positive integer", code="INVALID_TIMEOUT")
    try:
        timeout_ms = int(value)
    except (TypeError, ValueError) as exc:
        raise WsProtocolValidationError("timeout_ms must be a positive integer", code="INVALID_TIMEOUT") from exc
    if timeout_ms <= 0:
        raise WsProtocolValidationError("timeout_ms must be a positive integer", code="INVALID_TIMEOUT")
    return timeout_ms


def error_definition(code: str) -> JsonDict:
    normalized = str(code or "INTERNAL_ERROR").strip().upper() or "INTERNAL_ERROR"
    default = ERROR_TAXONOMY.get("INTERNAL_ERROR", {})
    record = ERROR_TAXONOMY.get(normalized, default)
    return {
        "code": normalized,
        "retryable": bool(record.get("retryable", False)),
        "category": str(record.get("category", "server")),
        "message": str(record.get("message", default.get("message", "Internal error"))),
    }


def is_retryable_error_code(code: str) -> bool:
    return bool(error_definition(code).get("retryable", False))


@dataclass(frozen=True)
class WsError:
    code: str
    message: str
    retryable: bool = False
    category: str | None = None
    details: JsonDict = field(default_factory=dict)

    def as_dict(self) -> JsonDict:
        payload: JsonDict = {
            "code": self.code,
            "message": self.message,
            "retryable": self.retryable,
        }
        if self.category:
            payload["category"] = self.category
        if self.details:
            payload["details"] = dict(self.details)
        return payload

    @classmethod
    def from_code(
        cls,
        code: str,
        *,
        message: str | None = None,
        details: Mapping[str, Any] | None = None,
        retryable: bool | None = None,
    ) -> "WsError":
        definition = error_definition(code)
        return cls(
            code=definition["code"],
            message=str(message or definition["message"]),
            retryable=definition["retryable"] if retryable is None else bool(retryable),
            category=str(definition.get("category") or ""),
            details=_copy_json_dict(details),
        )

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "WsError":
        code = _require_non_empty_str(payload.get("code"), code="INVALID_FRAME", field_name="error.code").upper()
        message = _require_non_empty_str(payload.get("message"), code="INVALID_FRAME", field_name="error.message")
        details = payload.get("details")
        if details is not None and not isinstance(details, Mapping):
            raise WsProtocolValidationError("error.details must be an object", code="INVALID_FRAME")
        retryable = payload.get("retryable")
        if retryable is None:
            retryable_flag = is_retryable_error_code(code)
        else:
            retryable_flag = bool(retryable)
        category = payload.get("category") or error_definition(code).get("category")
        return cls(
            code=code,
            message=message,
            retryable=retryable_flag,
            category=str(category) if category else None,
            details=_copy_json_dict(details),
        )


class WsProtocolValidationError(ValueError):
    def __init__(self, message: str, *, code: str = "INVALID_FRAME", details: Mapping[str, Any] | None = None) -> None:
        super().__init__(message)
        self.code = str(code or "INVALID_FRAME").upper()
        self.details = _copy_json_dict(details)

    def to_error(self) -> WsError:
        return WsError.from_code(self.code, message=str(self), details=self.details)


@dataclass(frozen=True)
class WsEnvelope:
    type: str
    request_id: str
    correlation_id: str | None = None
    action: str | None = None
    data: JsonDict = field(default_factory=dict)
    error: WsError | None = None
    final: bool = False
    idempotency_key: str | None = None
    timeout_ms: int | None = None
    ack: bool = False

    def as_dict(self) -> JsonDict:
        payload: JsonDict = {
            "type": self.type,
            "request_id": self.request_id,
            "correlation_id": self.correlation_id,
            "action": self.action,
            "data": dict(self.data),
            "error": self.error.as_dict() if self.error else None,
            "final": self.final,
            "ack": self.ack,
        }
        if self.idempotency_key is not None:
            payload["idempotency_key"] = self.idempotency_key
        if self.timeout_ms is not None:
            payload["timeout_ms"] = self.timeout_ms
        return payload

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "WsEnvelope":
        if not isinstance(payload, Mapping):
            raise WsProtocolValidationError("frame payload must be a JSON object", code="INVALID_FRAME")
        frame_type = _require_non_empty_str(payload.get("type"), code="INVALID_TYPE", field_name="type")
        if frame_type not in FRAME_TYPES:
            raise WsProtocolValidationError(f"unsupported frame type: {frame_type}", code="INVALID_TYPE")
        request_id = _require_non_empty_str(payload.get("request_id"), code="INVALID_REQUEST_ID", field_name="request_id")
        correlation_id = _normalize_optional_str(payload.get("correlation_id"))
        action = _normalize_optional_str(payload.get("action"))
        if frame_type in REQUEST_FRAME_TYPES and action is None:
            raise WsProtocolValidationError("request frames require action", code="INVALID_ACTION")
        data = payload.get("data")
        if data is None:
            normalized_data: JsonDict = {}
        elif isinstance(data, Mapping):
            normalized_data = dict(data)
        else:
            raise WsProtocolValidationError("data must be a JSON object", code="INVALID_FRAME")
        timeout_ms = _normalize_timeout_ms(payload.get("timeout_ms"))
        idempotency_key = _normalize_optional_str(payload.get("idempotency_key"))
        if payload.get("idempotency_key") is not None and idempotency_key is None:
            raise WsProtocolValidationError(
                "idempotency_key must be a non-empty string",
                code="INVALID_IDEMPOTENCY_KEY",
            )
        error_payload = payload.get("error")
        error = None
        if error_payload is not None:
            if not isinstance(error_payload, Mapping):
                raise WsProtocolValidationError("error must be a JSON object", code="INVALID_FRAME")
            error = WsError.from_dict(error_payload)
        if frame_type == "error" and error is None:
            raise WsProtocolValidationError("error frames require error payload", code="INVALID_FRAME")
        ack = bool(payload.get("ack", False))
        final = bool(payload.get("final", False))
        if frame_type == "ack":
            ack = True
        return cls(
            type=frame_type,
            request_id=request_id,
            correlation_id=correlation_id,
            action=action,
            data=normalized_data,
            error=error,
            final=final,
            idempotency_key=idempotency_key,
            timeout_ms=timeout_ms,
            ack=ack,
        )


def build_request(
    action: str,
    *,
    data: Mapping[str, Any] | None = None,
    request_id: str | None = None,
    correlation_id: str | None = None,
    idempotency_key: str | None = None,
    timeout_ms: int | None = None,
    ack: bool = False,
    frame_type: str = "command",
) -> JsonDict:
    if frame_type not in REQUEST_FRAME_TYPES:
        raise WsProtocolValidationError(f"unsupported request frame type: {frame_type}", code="INVALID_TYPE")
    envelope = WsEnvelope(
        type=frame_type,
        request_id=_require_non_empty_str(request_id or _new_request_id(), code="INVALID_REQUEST_ID", field_name="request_id"),
        correlation_id=_normalize_optional_str(correlation_id),
        action=_require_non_empty_str(action, code="INVALID_ACTION", field_name="action"),
        data=_copy_json_dict(data),
        error=None,
        final=False,
        idempotency_key=_normalize_optional_str(idempotency_key),
        timeout_ms=_normalize_timeout_ms(timeout_ms),
        ack=bool(ack),
    )
    if idempotency_key is not None and envelope.idempotency_key is None:
        raise WsProtocolValidationError("idempotency_key must be a non-empty string", code="INVALID_IDEMPOTENCY_KEY")
    return envelope.as_dict()


def build_ack(
    request_id: str,
    *,
    correlation_id: str | None = None,
    action: str | None = None,
    data: Mapping[str, Any] | None = None,
    final: bool = False,
) -> JsonDict:
    return WsEnvelope(
        type="ack",
        request_id=_require_non_empty_str(request_id, code="INVALID_REQUEST_ID", field_name="request_id"),
        correlation_id=_normalize_optional_str(correlation_id),
        action=_normalize_optional_str(action),
        data=_copy_json_dict(data),
        error=None,
        final=bool(final),
        ack=True,
    ).as_dict()


def build_response(
    request_id: str,
    *,
    action: str | None = None,
    correlation_id: str | None = None,
    data: Mapping[str, Any] | None = None,
    final: bool = True,
    ack: bool = False,
) -> JsonDict:
    return WsEnvelope(
        type="response",
        request_id=_require_non_empty_str(request_id, code="INVALID_REQUEST_ID", field_name="request_id"),
        correlation_id=_normalize_optional_str(correlation_id),
        action=_normalize_optional_str(action),
        data=_copy_json_dict(data),
        error=None,
        final=bool(final),
        ack=bool(ack),
    ).as_dict()


def build_event(
    request_id: str,
    *,
    action: str,
    data: Mapping[str, Any] | None = None,
    correlation_id: str | None = None,
    final: bool = False,
    frame_type: str = "event",
    ack: bool = False,
) -> JsonDict:
    if frame_type not in {"event", "snapshot", "delta"}:
        raise WsProtocolValidationError(f"unsupported event frame type: {frame_type}", code="INVALID_TYPE")
    return WsEnvelope(
        type=frame_type,
        request_id=_require_non_empty_str(request_id, code="INVALID_REQUEST_ID", field_name="request_id"),
        correlation_id=_normalize_optional_str(correlation_id),
        action=_require_non_empty_str(action, code="INVALID_ACTION", field_name="action"),
        data=_copy_json_dict(data),
        error=None,
        final=bool(final),
        ack=bool(ack),
    ).as_dict()


def build_error(
    request_id: str,
    code: str,
    *,
    action: str | None = None,
    correlation_id: str | None = None,
    message: str | None = None,
    details: Mapping[str, Any] | None = None,
    retryable: bool | None = None,
    final: bool = True,
) -> JsonDict:
    return WsEnvelope(
        type="error",
        request_id=_require_non_empty_str(request_id, code="INVALID_REQUEST_ID", field_name="request_id"),
        correlation_id=_normalize_optional_str(correlation_id),
        action=_normalize_optional_str(action),
        data={},
        error=WsError.from_code(code, message=message, details=details, retryable=retryable),
        final=bool(final),
        ack=False,
    ).as_dict()


def parse_json_frame(raw: str, *, normalize_chat: bool = False) -> WsEnvelope:
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise WsProtocolValidationError("frame payload is not valid JSON", code="INVALID_JSON") from exc
    return parse_envelope(payload, normalize_chat=normalize_chat)


def parse_envelope(frame: Mapping[str, Any], *, normalize_chat: bool = False) -> WsEnvelope:
    normalized = normalize_compact_chat_frame(frame) if normalize_chat else frame
    return WsEnvelope.from_dict(normalized)


def normalize_compact_chat_frame(
    frame: Mapping[str, Any],
    *,
    request_id_factory: RequestIdFactory | None = None,
) -> JsonDict:
    if not isinstance(frame, Mapping):
        raise WsProtocolValidationError("frame payload must be a JSON object", code="INVALID_FRAME")
    if frame.get("type") and frame.get("action"):
        return dict(frame)

    request_id_builder = request_id_factory or _new_request_id
    data = dict(frame.get("data", {})) if isinstance(frame.get("data"), Mapping) else {}

    message = frame.get("u") or frame.get("message") or frame.get("text")
    session_id = frame.get("m") or frame.get("session_id")
    user_id = frame.get("v") or frame.get("user_id")
    source = frame.get("s") or frame.get("source")
    provider = frame.get("o") or frame.get("provider")
    cost_tier = frame.get("cost_tier") or frame.get("tier")
    model = frame.get("model") or frame.get("requested_model")
    correlation_id = frame.get("c") or frame.get("correlation_id")
    request_id = frame.get("r") or frame.get("request_id") or request_id_builder()

    compact_fields = {
        "message": message,
        "session_id": session_id,
        "user_id": user_id,
        "source": source,
        "provider": provider,
        "priority": frame.get("priority"),
        "cost_tier": cost_tier,
        "model": model,
        "complexity": frame.get("complexity"),
    }
    for key, value in compact_fields.items():
        if value is not None:
            data.setdefault(key, value)

    reserved_keys = {
        "type",
        "action",
        "data",
        "error",
        "final",
        "ack",
        "timeout_ms",
        "idempotency_key",
        "request_id",
        "correlation_id",
        "r",
        "c",
        "u",
        "m",
        "v",
        "s",
        "o",
        "message",
        "text",
        "session_id",
        "user_id",
        "source",
        "provider",
        "tier",
        "cost_tier",
        "model",
        "requested_model",
        "priority",
        "complexity",
    }
    for key, value in frame.items():
        if key not in reserved_keys and key not in data:
            data[key] = value

    return {
        "type": str(frame.get("type") or "command"),
        "request_id": str(request_id),
        "correlation_id": _normalize_optional_str(correlation_id) or str(request_id),
        "action": str(frame.get("action") or "chat.submit"),
        "data": data,
        "error": frame.get("error"),
        "final": bool(frame.get("final", False)),
        "ack": bool(frame.get("ack", False)),
        "timeout_ms": frame.get("timeout_ms"),
        "idempotency_key": frame.get("idempotency_key"),
    }


__all__ = [
    "ERROR_TAXONOMY",
    "FRAME_TYPES",
    "REQUEST_FRAME_TYPES",
    "SERVER_FRAME_TYPES",
    "WsEnvelope",
    "WsError",
    "WsProtocolValidationError",
    "build_ack",
    "build_error",
    "build_event",
    "build_request",
    "build_response",
    "error_definition",
    "is_retryable_error_code",
    "normalize_compact_chat_frame",
    "parse_envelope",
    "parse_json_frame",
]
