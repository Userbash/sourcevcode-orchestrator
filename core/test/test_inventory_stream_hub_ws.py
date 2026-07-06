from __future__ import annotations

import asyncio

from core.core.inventory_stream_hub import InventoryStreamHub


def _snapshot(*, local_status: str, models: list[str], include_openai: bool = True, updated_at: int = 1) -> dict:
    providers = {
        "local_llm": {"provider": "local_llm", "status": local_status},
    }
    runtime_providers = {
        "local_llm": {"provider": "local_llm", "status": local_status, "models": [{"model_name": name} for name in models]},
    }
    if include_openai:
        providers["openai"] = {"provider": "openai", "status": "ready"}
        runtime_providers["openai"] = {"provider": "openai", "status": "ready", "models": [{"model_name": "gpt-5.5"}]}
    by_model = {name: {"provider": "local_llm", "resident": local_status == "ready"} for name in models}
    by_provider = {"local_llm": list(models)}
    provider_counts = {"local_llm": len(models)}
    if include_openai:
        by_model["gpt-5.5"] = {"provider": "openai", "resident": False}
        by_provider["openai"] = ["gpt-5.5"]
        provider_counts["openai"] = 1
    return {
        "updated_at": updated_at,
        "providers": providers,
        "summary": {"provider_count": len(providers)},
        "runtime_inventory": {
            "generated_at": updated_at,
            "providers": runtime_providers,
            "summary": {"provider_count": len(runtime_providers)},
        },
        "model_index": {
            "updated_at": updated_at,
            "total_models": len(by_model),
            "provider_counts": provider_counts,
            "by_model": by_model,
            "by_provider": by_provider,
        },
    }


async def _collect_runtime_delta() -> None:
    hub = InventoryStreamHub()
    hub.publish(_snapshot(local_status="offline", models=["qwen2.5:32b"]))

    queue, initial = hub.subscribe(InventoryStreamHub.TOPIC_PROVIDER_RUNTIME_INVENTORY, queue_maxsize=4)
    assert initial is not None
    assert initial["topic"] == InventoryStreamHub.TOPIC_PROVIDER_RUNTIME_INVENTORY
    assert initial["kind"] == "snapshot"
    assert initial["version"] == 1
    assert initial["data"]["providers"]["local_llm"]["status"] == "offline"

    hub.publish(_snapshot(local_status="ready", models=["qwen2.5:32b", "qwen2.5:72b"], include_openai=False, updated_at=2))

    event = await asyncio.wait_for(queue.get(), timeout=1.0)
    assert event["topic"] == InventoryStreamHub.TOPIC_PROVIDER_RUNTIME_INVENTORY
    assert event["kind"] == "delta"
    assert event["version"] == 2
    assert event["data"]["providers"]["local_llm"]["status"] == "ready"
    assert event["delta"]["providers"]["upsert"]["local_llm"]["status"] == "ready"
    assert event["delta"]["providers"]["remove"] == ["openai"]


async def _unsubscribe_and_model_index() -> None:
    hub = InventoryStreamHub()
    hub.publish(_snapshot(local_status="offline", models=["qwen2.5:32b"]))

    queue, initial = hub.subscribe(InventoryStreamHub.TOPIC_MODEL_INDEX)
    assert initial is not None
    assert initial["data"]["by_model"]["qwen2.5:32b"]["provider"] == "local_llm"
    assert hub.snapshot(InventoryStreamHub.TOPIC_PROVIDER_INVENTORY)["providers"]["openai"]["provider"] == "openai"

    hub.unsubscribe(queue)
    hub.publish(_snapshot(local_status="ready", models=["qwen2.5:72b"], updated_at=2))
    await asyncio.sleep(0)

    assert queue.empty()
    assert hub.current_version(InventoryStreamHub.TOPIC_MODEL_INDEX) == 2
    assert hub.find_model("qwen2.5:72b")["provider"] == "local_llm"
    assert hub.provider_models("local") == ["qwen2.5:72b"]


async def _backpressure_keeps_latest_deltas() -> None:
    hub = InventoryStreamHub()
    queue, initial = hub.subscribe(
        InventoryStreamHub.TOPIC_PROVIDER_RUNTIME_INVENTORY,
        queue_maxsize=2,
        include_snapshot=False,
    )
    assert initial is None

    hub.publish(_snapshot(local_status="booting", models=["m1"], updated_at=1))
    hub.publish(_snapshot(local_status="warming", models=["m2"], updated_at=2))
    hub.publish(_snapshot(local_status="ready", models=["m3"], updated_at=3))
    await asyncio.sleep(0)

    events = [queue.get_nowait(), queue.get_nowait()]
    assert [event["version"] for event in events] == [2, 3]
    assert [event["data"]["providers"]["local_llm"]["status"] for event in events] == ["warming", "ready"]


def test_inventory_runtime_subscription_emits_snapshot_then_delta():
    asyncio.run(_collect_runtime_delta())


def test_inventory_topics_support_unsubscribe_and_model_index_views():
    asyncio.run(_unsubscribe_and_model_index())


def test_inventory_subscription_backpressure_drops_oldest_delta():
    asyncio.run(_backpressure_keeps_latest_deltas())
