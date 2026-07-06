from __future__ import annotations

import asyncio

from core.core.runtime_event_stream_hub import RuntimeEventStreamHub


async def _next_queue_item(queue: asyncio.Queue[dict]):
    return await asyncio.wait_for(queue.get(), timeout=0.2)


async def _drain_loop():
    await asyncio.sleep(0)


def test_subscribe_emits_snapshot_then_versioned_delta():
    async def scenario():
        hub = RuntimeEventStreamHub()
        queue, initial = hub.subscribe()

        assert initial["schema_version"] == "runtime_events.ws.v1"
        assert initial["stream"] == "runtime_events"
        assert initial["event"] == "snapshot"
        assert initial["subscription_id"].startswith("runtime-events-")
        assert initial["snapshot"] == {"agents": {}, "workflows": {}, "events": []}
        assert initial["data"] == initial["snapshot"]

        hub.publish_agent_event("coder-1", {"status": "ready", "source": "probe"})
        await _drain_loop()
        event = await _next_queue_item(queue)

        assert event["subscription_id"] == initial["subscription_id"]
        assert event["event"] == "delta"
        assert event["version"] == 1
        assert event["previous_version"] == 0
        assert event["delta"]["kind"] == "agent"
        assert event["delta"]["agent_id"] == "coder-1"
        assert event["delta"]["row"]["status"] == "ready"
        assert event["snapshot"]["agents"]["coder-1"]["status"] == "ready"
        assert event["data"]["agents"]["coder-1"]["source"] == "probe"

    asyncio.run(scenario())


def test_subscription_registry_and_unsubscribe_support_queue_and_id():
    async def scenario():
        hub = RuntimeEventStreamHub()
        queue, initial = hub.subscribe()

        assert hub.subscription_count() == 1
        assert hub.subscriptions()[0]["subscription_id"] == initial["subscription_id"]

        assert hub.unsubscribe(initial["subscription_id"]) is True
        assert hub.subscription_count() == 0
        assert hub.unsubscribe(queue) is False

        queue2, initial2 = hub.subscribe()
        assert hub.subscription_count() == 1
        assert hub.unsubscribe(queue2) is True
        assert hub.subscription_count() == 0
        assert initial2["subscription_id"] != initial["subscription_id"]

    asyncio.run(scenario())


def test_stream_unregisters_subscription_on_generator_close():
    async def scenario():
        hub = RuntimeEventStreamHub()
        stream = hub.stream()
        first = await stream.__anext__()

        assert first["event"] == "snapshot"
        assert hub.subscription_count() == 1

        await stream.aclose()

        assert hub.subscription_count() == 0

    asyncio.run(scenario())


def test_backpressure_drops_stale_events_and_keeps_latest_delta():
    async def scenario():
        hub = RuntimeEventStreamHub(queue_size=1)
        queue, initial = hub.subscribe()

        for idx in range(3):
            hub.publish_workflow_event("wf-1", {"step": idx})
        await _drain_loop()
        event = await _next_queue_item(queue)

        assert initial["subscription_id"] == event["subscription_id"]
        assert event["event"] == "delta"
        assert event["version"] == 3
        assert event["delta"]["kind"] == "workflow"
        assert event["delta"]["row"]["step"] == 2
        assert event["snapshot"]["workflows"]["wf-1"]["step"] == 2
        assert event["dropped_events"] >= 1

    asyncio.run(scenario())


def test_current_event_returns_snapshot_envelope_with_latest_version():
    hub = RuntimeEventStreamHub()
    hub.publish_workflow_event("wf-live", {"status": "running"})

    event = hub.current_event()

    assert event["event"] == "snapshot"
    assert event["version"] == 1
    assert event["snapshot"]["workflows"]["wf-live"]["status"] == "running"
    assert event["data"] == event["snapshot"]
