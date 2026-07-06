from __future__ import annotations

import pytest

from core.core.ws_protocol import (
    WsProtocolValidationError,
    build_ack,
    build_error,
    build_event,
    build_request,
    build_response,
    error_definition,
    is_retryable_error_code,
    normalize_compact_chat_frame,
    parse_envelope,
    parse_json_frame,
)


def test_build_request_round_trip_preserves_mutation_metadata():
    frame = build_request(
        "local_llm/warm",
        data={"model_name": "qwen2.5:32b"},
        request_id="req-1",
        correlation_id="corr-1",
        idempotency_key="warm:req-1",
        timeout_ms=1500,
        ack=True,
    )

    envelope = parse_envelope(frame)

    assert envelope.type == "command"
    assert envelope.request_id == "req-1"
    assert envelope.correlation_id == "corr-1"
    assert envelope.action == "local_llm/warm"
    assert envelope.data == {"model_name": "qwen2.5:32b"}
    assert envelope.idempotency_key == "warm:req-1"
    assert envelope.timeout_ms == 1500
    assert envelope.ack is True
    assert envelope.final is False
    assert envelope.as_dict() == frame


def test_build_response_ack_and_terminal_event_shapes():
    ack = parse_envelope(build_ack("req-2", action="ai_kernel/ensure", data={"accepted": True}))
    progress = parse_envelope(
        build_event(
            "req-2",
            action="ai_kernel/ensure",
            data={"stage": "warming"},
            correlation_id="corr-2",
            frame_type="event",
        )
    )
    done = parse_envelope(build_response("req-2", action="ai_kernel/ensure", data={"ready": True}, final=True))

    assert ack.type == "ack"
    assert ack.ack is True
    assert ack.data["accepted"] is True
    assert progress.type == "event"
    assert progress.final is False
    assert done.type == "response"
    assert done.final is True
    assert done.data["ready"] is True


def test_build_error_uses_taxonomy_defaults():
    frame = build_error("req-3", "TIMEOUT", action="local_llm/connect", details={"timeout_ms": 5000})
    envelope = parse_envelope(frame)
    definition = error_definition("TIMEOUT")

    assert envelope.type == "error"
    assert envelope.final is True
    assert envelope.error is not None
    assert envelope.error.code == "TIMEOUT"
    assert envelope.error.retryable is True
    assert envelope.error.category == definition["category"]
    assert envelope.error.details == {"timeout_ms": 5000}
    assert is_retryable_error_code("TIMEOUT") is True
    assert is_retryable_error_code("UNSUPPORTED_ACTION") is False


def test_parse_rejects_command_without_action():
    with pytest.raises(WsProtocolValidationError) as exc:
        parse_envelope({"type": "command", "request_id": "req-4", "data": {}})

    assert exc.value.code == "INVALID_ACTION"


def test_parse_json_frame_rejects_invalid_json():
    with pytest.raises(WsProtocolValidationError) as exc:
        parse_json_frame("{not json}")

    assert exc.value.code == "INVALID_JSON"


def test_normalize_compact_chat_frame_promotes_legacy_shape():
    frame = normalize_compact_chat_frame(
        {
            "u": "hello",
            "m": "session-9",
            "v": "user-2",
            "s": "websocket",
            "o": "openai",
            "tier": "cheap",
            "requested_model": "gpt-5.5",
            "priority": "high",
            "complexity": "low",
            "custom_flag": True,
        },
        request_id_factory=lambda: "generated-1",
    )

    envelope = parse_envelope(frame)

    assert envelope.type == "command"
    assert envelope.request_id == "generated-1"
    assert envelope.correlation_id == "generated-1"
    assert envelope.action == "chat.submit"
    assert envelope.data == {
        "message": "hello",
        "session_id": "session-9",
        "user_id": "user-2",
        "source": "websocket",
        "provider": "openai",
        "priority": "high",
        "cost_tier": "cheap",
        "model": "gpt-5.5",
        "complexity": "low",
        "custom_flag": True,
    }


def test_invalid_timeout_and_idempotency_are_rejected():
    with pytest.raises(WsProtocolValidationError) as timeout_exc:
        build_request("local_llm/warm", request_id="req-5", timeout_ms=0)
    with pytest.raises(WsProtocolValidationError) as idem_exc:
        build_request("local_llm/warm", request_id="req-5", idempotency_key=" ")

    assert timeout_exc.value.code == "INVALID_TIMEOUT"
    assert idem_exc.value.code == "INVALID_IDEMPOTENCY_KEY"
