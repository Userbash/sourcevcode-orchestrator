from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from core.core.contracts import ContractArbiterService
from core.core.contracts import ContractStatus
from core.core.contracts import ContractValidationError
from core.core.contracts_storage import InMemoryContractsStorage


def test_contract_lifecycle_records_append_only_event_log():
    now = datetime(2026, 7, 7, 12, 0, tzinfo=UTC)
    store = InMemoryContractsStorage()
    service = ContractArbiterService(store)

    contract = service.create_contract(
        party_a_id="buyer-1",
        party_b_id="seller-1",
        arbiter_id="arbiter-1",
        subject="dataset exchange",
        terms_text="seller sends dataset after escrow is locked",
        ttl_seconds=1800,
        execution_ttl_seconds=7200,
        contract_id="ctr-1",
        now=now,
    )
    contract = service.open_for_signing("ctr-1", arbiter_id="arbiter-1", now=now + timedelta(minutes=1))
    contract = service.sign_contract("ctr-1", party_id="buyer-1", signature="sig-a", now=now + timedelta(minutes=2))
    assert contract.status == ContractStatus.PENDING_B
    contract = service.sign_contract("ctr-1", party_id="seller-1", signature="sig-b", now=now + timedelta(minutes=3))
    assert contract.status == ContractStatus.ACTIVE

    contract = service.lock_escrow("ctr-1", arbiter_id="arbiter-1", now=now + timedelta(minutes=40))
    contract = service.mark_data_ready("ctr-1", actor_id="seller-1", now=now + timedelta(minutes=50))
    contract = service.release_data("ctr-1", arbiter_id="arbiter-1", now=now + timedelta(minutes=55))
    contract = service.settle_contract("ctr-1", arbiter_id="arbiter-1", now=now + timedelta(minutes=60))

    assert contract.status == ContractStatus.SETTLED
    assert contract.effective_until == now + timedelta(minutes=3, seconds=7200)

    events = store.events_for_contract("ctr-1")
    assert [event["event_type"] for event in events] == [
        "created",
        "signing_opened",
        "signed",
        "signed",
        "escrow_locked",
        "data_ready",
        "data_released",
        "settled",
    ]
    assert events[1]["prev_event_hash"] == events[0]["event_hash"]
    assert events[-1]["to_status"] == "settled"


def test_contract_expires_when_signature_window_runs_out():
    now = datetime(2026, 7, 7, 12, 0, tzinfo=UTC)
    store = InMemoryContractsStorage()
    service = ContractArbiterService(store)

    service.create_contract(
        party_a_id="buyer-2",
        party_b_id="seller-2",
        arbiter_id="arbiter-2",
        subject="document exchange",
        terms_text="both parties sign within ten minutes",
        ttl_seconds=600,
        execution_ttl_seconds=3600,
        contract_id="ctr-expire",
        now=now,
    )
    service.open_for_signing("ctr-expire", arbiter_id="arbiter-2", now=now + timedelta(minutes=1))

    expired = service.expire_contracts(now=now + timedelta(minutes=11))

    assert [contract.contract_id for contract in expired] == ["ctr-expire"]
    assert store.get_contract("ctr-expire").status == ContractStatus.EXPIRED
    assert store.events_for_contract("ctr-expire")[-1]["event_type"] == "expired"


def test_signature_verification_failure_voids_contract_and_blocks_future_steps():
    now = datetime(2026, 7, 7, 12, 0, tzinfo=UTC)
    store = InMemoryContractsStorage()
    service = ContractArbiterService(
        store,
        signature_verifier=lambda _contract, party_id, signature: not (party_id == "seller-3" and signature == "bad-signature"),
    )

    service.create_contract(
        party_a_id="buyer-3",
        party_b_id="seller-3",
        arbiter_id="arbiter-3",
        subject="api access",
        terms_text="seller enables api access after both signatures",
        ttl_seconds=1800,
        execution_ttl_seconds=1800,
        contract_id="ctr-void",
        now=now,
    )
    service.open_for_signing("ctr-void", arbiter_id="arbiter-3", now=now + timedelta(minutes=1))
    service.sign_contract("ctr-void", party_id="buyer-3", signature="good-signature", now=now + timedelta(minutes=2))

    contract = service.sign_contract("ctr-void", party_id="seller-3", signature="bad-signature", now=now + timedelta(minutes=3))

    assert contract.status == ContractStatus.VOID
    assert store.events_for_contract("ctr-void")[-1]["event_type"] == "voided"
    with pytest.raises(ContractValidationError):
        service.lock_escrow("ctr-void", arbiter_id="arbiter-3", now=now + timedelta(minutes=4))


def test_active_contract_uses_execution_deadline_after_signing_window_has_passed():
    now = datetime(2026, 7, 7, 12, 0, tzinfo=UTC)
    store = InMemoryContractsStorage()
    service = ContractArbiterService(store)

    service.create_contract(
        party_a_id="buyer-4",
        party_b_id="seller-4",
        arbiter_id="arbiter-4",
        subject="model weights transfer",
        terms_text="release weights after escrow",
        ttl_seconds=300,
        execution_ttl_seconds=3600,
        contract_id="ctr-window",
        now=now,
    )
    service.open_for_signing("ctr-window", arbiter_id="arbiter-4", now=now + timedelta(seconds=30))
    service.sign_contract("ctr-window", party_id="buyer-4", signature="sig-a", now=now + timedelta(seconds=60))
    service.sign_contract("ctr-window", party_id="seller-4", signature="sig-b", now=now + timedelta(seconds=120))

    contract = service.lock_escrow("ctr-window", arbiter_id="arbiter-4", now=now + timedelta(minutes=20))

    assert contract.status == ContractStatus.ESCROW_LOCKED
    assert service.contract_is_valid("ctr-window", now=now + timedelta(minutes=20)) is True
