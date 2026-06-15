import asyncio

from core.core.models import P2PMessage, P2PMessageType, TaskPayload, encapsulate
from core.core.rabbitmq_bus import RabbitMQBus


def test_rabbitmq_bus_serializes_task_envelope_payload():
    envelope = encapsulate(TaskPayload("ship websocket fix"), metadata={"target_agent": "coder-agent"})

    payload = RabbitMQBus._serialize_message(envelope)

    assert payload["kind"] == "envelope"
    assert payload["payload"]["task_id"] == envelope.task_id
    assert payload["payload"]["target_agent"] == "coder-agent"


def test_rabbitmq_bus_serializes_p2p_message_payload():
    message = P2PMessage(task_id="t1", from_agent="a1", to_agent="a2", message_type=P2PMessageType.RESULT, payload={"ok": True})

    payload = RabbitMQBus._serialize_message(message)

    assert payload["kind"] == "p2p"
    assert payload["payload"]["task_id"] == "t1"
    assert payload["payload"]["payload"]["ok"] is True


def test_rabbitmq_bus_run_async_result_works_inside_running_loop():
    bus = RabbitMQBus()
    bus._enabled = True

    async def _sample() -> str:
        await asyncio.sleep(0)
        return "ok"

    async def _run() -> str:
        return bus._run_async_result(_sample(), default="fallback")

    assert asyncio.run(_run()) == "ok"
