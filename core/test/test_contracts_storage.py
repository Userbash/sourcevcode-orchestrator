from __future__ import annotations

from datetime import UTC, datetime, timedelta

from core.core.contracts_storage import InMemoryContractsStorage


def test_save_get_and_filters_return_copied_snapshots():
    store = InMemoryContractsStorage()
    soon = datetime(2026, 7, 10, 12, 0, tzinfo=UTC)

    saved = store.save(
        "contract-1",
        {
            "status": "active",
            "expires_at": soon.isoformat(),
            "terms": {"amount": 100},
        },
    )
    saved["terms"]["amount"] = 0

    fetched = store.get("contract-1")

    assert fetched == {
        "contract_id": "contract-1",
        "status": "active",
        "expires_at": soon.isoformat(),
        "terms": {"amount": 100},
    }

    status_rows = store.list_by_status("active")
    status_rows[0]["terms"]["amount"] = 50

    expiring = store.list_expiring(soon + timedelta(minutes=1))
    expiring[0]["terms"]["amount"] = 75

    assert store.get("contract-1")["terms"]["amount"] == 100


def test_list_expiring_filters_and_orders_by_expiry_then_id():
    store = InMemoryContractsStorage()
    threshold = datetime(2026, 7, 12, 0, 0, tzinfo=UTC)

    store.save("c-late", {"status": "active", "expires_at": "2026-07-11T12:00:00+00:00"})
    store.save("c-early-b", {"status": "pending", "expires_at": "2026-07-09T09:00:00+00:00"})
    store.save("c-early-a", {"status": "active", "expires_at": "2026-07-09T09:00:00+00:00"})
    store.save("c-open", {"status": "draft"})
    store.save("c-future", {"status": "active", "expires_at": "2026-07-20T09:00:00+00:00"})

    rows = store.list_expiring(threshold)

    assert [row["contract_id"] for row in rows] == ["c-early-a", "c-early-b", "c-late"]


def test_append_event_maintains_append_only_per_contract_logs():
    store = InMemoryContractsStorage()
    timestamp = datetime(2026, 7, 7, 18, 30, tzinfo=UTC)

    first = store.append_event("contract-7", "created", {"actor": "arbiter"}, logged_at=timestamp)
    second = store.append_event("contract-7", "status_changed", {"status": "active"})
    other = store.append_event("contract-8", "created", {"actor": "peer"})

    first["payload"]["actor"] = "mutated"
    events = store.events_for_contract("contract-7")
    events[1]["payload"]["status"] = "mutated"

    assert first["sequence"] == 1
    assert second["sequence"] == 2
    assert other["sequence"] == 1
    assert [event["event_type"] for event in store.events_for_contract("contract-7")] == ["created", "status_changed"]
    assert store.events_for_contract("contract-7")[0]["payload"] == {"actor": "arbiter"}
    assert store.events_for_contract("contract-7")[0]["logged_at"] == timestamp.isoformat()
    assert store.events_for_contract("contract-8") == [other]
