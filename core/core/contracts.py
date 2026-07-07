from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from enum import Enum
from hashlib import sha256
from typing import Any, Callable, Protocol
from uuid import uuid4


class ContractStatus(str, Enum):
    DRAFT = "draft"
    PENDING_A = "pending_a"
    PENDING_B = "pending_b"
    ACTIVE = "active"
    ESCROW_LOCKED = "escrow_locked"
    DATA_READY = "data_ready"
    DATA_RELEASED = "data_released"
    SETTLED = "settled"
    CANCELLED = "cancelled"
    EXPIRED = "expired"
    VOID = "void"


class ContractEventType(str, Enum):
    CREATED = "created"
    SIGNING_OPENED = "signing_opened"
    SIGNED = "signed"
    ESCROW_LOCKED = "escrow_locked"
    DATA_READY = "data_ready"
    DATA_RELEASED = "data_released"
    SETTLED = "settled"
    CANCELLED = "cancelled"
    EXPIRED = "expired"
    VOIDED = "voided"


TERMINAL_CONTRACT_STATUSES = {
    ContractStatus.SETTLED,
    ContractStatus.CANCELLED,
    ContractStatus.EXPIRED,
    ContractStatus.VOID,
}

ACTIVE_CONTRACT_STATUSES = {
    ContractStatus.PENDING_A,
    ContractStatus.PENDING_B,
    ContractStatus.ACTIVE,
    ContractStatus.ESCROW_LOCKED,
    ContractStatus.DATA_READY,
    ContractStatus.DATA_RELEASED,
}


class ContractValidationError(ValueError):
    pass


@dataclass(slots=True, frozen=True)
class ContractSignature:
    party_id: str
    signed_at: datetime
    signature: str
    key_id: str | None = None


@dataclass(slots=True, frozen=True)
class ContractRecord:
    contract_id: str
    version: int
    party_a_id: str
    party_b_id: str
    arbiter_id: str
    subject: str
    terms_text: str
    terms_hash: str
    created_at: datetime
    expires_at: datetime
    ttl_seconds: int
    status: ContractStatus = ContractStatus.DRAFT
    effective_from: datetime | None = None
    effective_until: datetime | None = None
    execution_ttl_seconds: int = 7 * 24 * 60 * 60
    escrow_required: bool = True
    data_transfer_mode: str = "escrow_release"
    sign_a: ContractSignature | None = None
    sign_b: ContractSignature | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    audit_chain_hash: str = ""
    last_event_at: datetime | None = None
    cancelled_reason: str | None = None
    void_reason: str | None = None

    def is_terminal(self) -> bool:
        return self.status in TERMINAL_CONTRACT_STATUSES

    def is_effective(self, *, now: datetime | None = None) -> bool:
        now_value = _ensure_utc(now or datetime.now(UTC))
        return (
            self.status in {
                ContractStatus.ACTIVE,
                ContractStatus.ESCROW_LOCKED,
                ContractStatus.DATA_READY,
                ContractStatus.DATA_RELEASED,
            }
            and self.effective_from is not None
            and self.effective_until is not None
            and self.effective_from <= now_value <= self.effective_until
        )


@dataclass(slots=True, frozen=True)
class ContractEvent:
    event_id: str
    contract_id: str
    event_type: ContractEventType
    actor_id: str
    actor_role: str
    from_status: str
    to_status: str
    payload: dict[str, Any]
    created_at: datetime
    prev_event_hash: str
    event_hash: str


class ContractStore(Protocol):
    def save_contract(self, contract: ContractRecord) -> ContractRecord: ...
    def get_contract(self, contract_id: str) -> ContractRecord | None: ...
    def append_event(self, event: ContractEvent) -> ContractEvent: ...
    def events_for_contract(self, contract_id: str) -> list[ContractEvent]: ...
    def list_by_status(self, statuses: set[ContractStatus]) -> list[ContractRecord]: ...
    def list_expiring_before(self, deadline: datetime, *, statuses: set[ContractStatus] | None = None) -> list[ContractRecord]: ...


def compute_terms_hash(terms_text: str) -> str:
    normalized = str(terms_text).strip()
    if not normalized:
        raise ContractValidationError("terms_text is required")
    return sha256(normalized.encode("utf-8")).hexdigest()


def _ensure_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _event_hash(
    *,
    contract_id: str,
    event_type: ContractEventType,
    actor_id: str,
    actor_role: str,
    from_status: str,
    to_status: str,
    payload: dict[str, Any],
    created_at: datetime,
    prev_event_hash: str,
) -> str:
    payload_repr = repr(sorted(payload.items()))
    material = "|".join(
        [
            contract_id,
            event_type.value,
            actor_id,
            actor_role,
            from_status,
            to_status,
            payload_repr,
            created_at.isoformat(),
            prev_event_hash,
        ]
    )
    return sha256(material.encode("utf-8")).hexdigest()


class ContractArbiterService:
    def __init__(
        self,
        store: ContractStore,
        *,
        signature_verifier: Callable[[ContractRecord, str, str], bool] | None = None,
    ) -> None:
        self.store = store
        self.signature_verifier = signature_verifier or (lambda _contract, _party_id, _signature: True)

    def create_contract(
        self,
        *,
        party_a_id: str,
        party_b_id: str,
        arbiter_id: str,
        subject: str,
        terms_text: str,
        ttl_seconds: int = 24 * 60 * 60,
        execution_ttl_seconds: int = 7 * 24 * 60 * 60,
        escrow_required: bool = True,
        data_transfer_mode: str = "escrow_release",
        metadata: dict[str, Any] | None = None,
        contract_id: str | None = None,
        now: datetime | None = None,
    ) -> ContractRecord:
        now_value = _ensure_utc(now or datetime.now(UTC))
        if ttl_seconds <= 0:
            raise ContractValidationError("ttl_seconds must be positive")
        if execution_ttl_seconds <= 0:
            raise ContractValidationError("execution_ttl_seconds must be positive")
        for field_name, field_value in {
            "party_a_id": party_a_id,
            "party_b_id": party_b_id,
            "arbiter_id": arbiter_id,
            "subject": subject,
        }.items():
            if not str(field_value).strip():
                raise ContractValidationError(f"{field_name} is required")

        record = ContractRecord(
            contract_id=contract_id or f"contract-{uuid4().hex}",
            version=1,
            party_a_id=str(party_a_id).strip(),
            party_b_id=str(party_b_id).strip(),
            arbiter_id=str(arbiter_id).strip(),
            subject=str(subject).strip(),
            terms_text=str(terms_text).strip(),
            terms_hash=compute_terms_hash(terms_text),
            created_at=now_value,
            expires_at=now_value + timedelta(seconds=ttl_seconds),
            ttl_seconds=int(ttl_seconds),
            execution_ttl_seconds=int(execution_ttl_seconds),
            escrow_required=bool(escrow_required),
            data_transfer_mode=str(data_transfer_mode).strip() or "escrow_release",
            metadata=dict(metadata or {}),
        )
        saved = self.store.save_contract(record)
        return self._log_transition(
            saved,
            event_type=ContractEventType.CREATED,
            actor_id=arbiter_id,
            actor_role="arbiter",
            from_status="",
            to_status=saved.status.value,
            payload={"terms_hash": saved.terms_hash, "expires_at": saved.expires_at.isoformat()},
            event_time=now_value,
        )

    def open_for_signing(self, contract_id: str, *, arbiter_id: str, now: datetime | None = None) -> ContractRecord:
        contract = self._require_contract(contract_id)
        self._assert_status(contract, {ContractStatus.DRAFT})
        now_value = self._ensure_within_signing_window(contract, now=now)
        updated = replace(contract, status=ContractStatus.PENDING_A, version=contract.version + 1)
        saved = self.store.save_contract(updated)
        return self._log_transition(
            saved,
            event_type=ContractEventType.SIGNING_OPENED,
            actor_id=arbiter_id,
            actor_role="arbiter",
            from_status=contract.status.value,
            to_status=saved.status.value,
            payload={},
            event_time=now_value,
        )

    def sign_contract(
        self,
        contract_id: str,
        *,
        party_id: str,
        signature: str,
        key_id: str | None = None,
        now: datetime | None = None,
    ) -> ContractRecord:
        contract = self._require_contract(contract_id)
        self._assert_status(contract, {ContractStatus.PENDING_A, ContractStatus.PENDING_B})
        now_value = self._ensure_within_signing_window(contract, now=now)
        party = str(party_id).strip()
        if contract.terms_hash != compute_terms_hash(contract.terms_text):
            return self.void_contract(contract_id, actor_id=contract.arbiter_id, reason="terms_hash_mismatch", now=now_value)
        if not self.signature_verifier(contract, party, signature):
            return self.void_contract(contract_id, actor_id=contract.arbiter_id, reason="signature_verification_failed", now=now_value)

        if contract.status == ContractStatus.PENDING_A:
            if party != contract.party_a_id:
                raise ContractValidationError("party_a must sign first")
            updated = replace(
                contract,
                status=ContractStatus.PENDING_B,
                sign_a=ContractSignature(party_id=party, signed_at=now_value, signature=signature, key_id=key_id),
                version=contract.version + 1,
            )
        else:
            if party != contract.party_b_id:
                raise ContractValidationError("party_b must sign second")
            effective_from = now_value
            effective_until = now_value + timedelta(seconds=contract.execution_ttl_seconds)
            updated = replace(
                contract,
                status=ContractStatus.ACTIVE,
                sign_b=ContractSignature(party_id=party, signed_at=now_value, signature=signature, key_id=key_id),
                effective_from=effective_from,
                effective_until=effective_until,
                version=contract.version + 1,
            )

        saved = self.store.save_contract(updated)
        return self._log_transition(
            saved,
            event_type=ContractEventType.SIGNED,
            actor_id=party,
            actor_role="party",
            from_status=contract.status.value,
            to_status=saved.status.value,
            payload={"key_id": key_id or "", "effective_until": saved.effective_until.isoformat() if saved.effective_until else ""},
            event_time=now_value,
        )

    def lock_escrow(self, contract_id: str, *, arbiter_id: str, now: datetime | None = None) -> ContractRecord:
        contract = self._require_contract(contract_id)
        self._assert_status(contract, {ContractStatus.ACTIVE})
        now_value = self._ensure_within_execution_window(contract, now=now)
        if not contract.escrow_required:
            raise ContractValidationError("escrow is not required for this contract")
        updated = replace(contract, status=ContractStatus.ESCROW_LOCKED, version=contract.version + 1)
        saved = self.store.save_contract(updated)
        return self._log_transition(
            saved,
            event_type=ContractEventType.ESCROW_LOCKED,
            actor_id=arbiter_id,
            actor_role="arbiter",
            from_status=contract.status.value,
            to_status=saved.status.value,
            payload={},
            event_time=now_value,
        )

    def mark_data_ready(self, contract_id: str, *, actor_id: str, now: datetime | None = None) -> ContractRecord:
        contract = self._require_contract(contract_id)
        expected_statuses = {ContractStatus.ESCROW_LOCKED} if contract.escrow_required else {ContractStatus.ACTIVE}
        self._assert_status(contract, expected_statuses)
        now_value = self._ensure_within_execution_window(contract, now=now)
        updated = replace(contract, status=ContractStatus.DATA_READY, version=contract.version + 1)
        saved = self.store.save_contract(updated)
        return self._log_transition(
            saved,
            event_type=ContractEventType.DATA_READY,
            actor_id=actor_id,
            actor_role="party",
            from_status=contract.status.value,
            to_status=saved.status.value,
            payload={},
            event_time=now_value,
        )

    def release_data(self, contract_id: str, *, arbiter_id: str, now: datetime | None = None) -> ContractRecord:
        contract = self._require_contract(contract_id)
        self._assert_status(contract, {ContractStatus.DATA_READY})
        now_value = self._ensure_within_execution_window(contract, now=now)
        updated = replace(contract, status=ContractStatus.DATA_RELEASED, version=contract.version + 1)
        saved = self.store.save_contract(updated)
        return self._log_transition(
            saved,
            event_type=ContractEventType.DATA_RELEASED,
            actor_id=arbiter_id,
            actor_role="arbiter",
            from_status=contract.status.value,
            to_status=saved.status.value,
            payload={},
            event_time=now_value,
        )

    def settle_contract(self, contract_id: str, *, arbiter_id: str, now: datetime | None = None) -> ContractRecord:
        contract = self._require_contract(contract_id)
        self._assert_status(contract, {ContractStatus.DATA_RELEASED})
        now_value = self._ensure_within_execution_window(contract, now=now)
        updated = replace(contract, status=ContractStatus.SETTLED, version=contract.version + 1)
        saved = self.store.save_contract(updated)
        return self._log_transition(
            saved,
            event_type=ContractEventType.SETTLED,
            actor_id=arbiter_id,
            actor_role="arbiter",
            from_status=contract.status.value,
            to_status=saved.status.value,
            payload={},
            event_time=now_value,
        )

    def cancel_contract(
        self,
        contract_id: str,
        *,
        actor_id: str,
        reason: str,
        now: datetime | None = None,
    ) -> ContractRecord:
        contract = self._require_contract(contract_id)
        self._assert_status(
            contract,
            {
                ContractStatus.DRAFT,
                ContractStatus.PENDING_A,
                ContractStatus.PENDING_B,
                ContractStatus.ACTIVE,
                ContractStatus.ESCROW_LOCKED,
                ContractStatus.DATA_READY,
            },
        )
        now_value = _ensure_utc(now or datetime.now(UTC))
        updated = replace(
            contract,
            status=ContractStatus.CANCELLED,
            cancelled_reason=str(reason).strip() or "cancelled",
            version=contract.version + 1,
        )
        saved = self.store.save_contract(updated)
        return self._log_transition(
            saved,
            event_type=ContractEventType.CANCELLED,
            actor_id=actor_id,
            actor_role="arbiter" if actor_id == contract.arbiter_id else "party",
            from_status=contract.status.value,
            to_status=saved.status.value,
            payload={"reason": saved.cancelled_reason or ""},
            event_time=now_value,
        )

    def void_contract(
        self,
        contract_id: str,
        *,
        actor_id: str,
        reason: str,
        now: datetime | None = None,
    ) -> ContractRecord:
        contract = self._require_contract(contract_id)
        if contract.is_terminal():
            raise ContractValidationError("contract is already terminal")
        now_value = _ensure_utc(now or datetime.now(UTC))
        updated = replace(
            contract,
            status=ContractStatus.VOID,
            void_reason=str(reason).strip() or "void",
            version=contract.version + 1,
        )
        saved = self.store.save_contract(updated)
        return self._log_transition(
            saved,
            event_type=ContractEventType.VOIDED,
            actor_id=actor_id,
            actor_role="arbiter",
            from_status=contract.status.value,
            to_status=saved.status.value,
            payload={"reason": saved.void_reason or ""},
            event_time=now_value,
        )

    def expire_contracts(self, *, now: datetime | None = None) -> list[ContractRecord]:
        now_value = _ensure_utc(now or datetime.now(UTC))
        expiring = self.store.list_expiring_before(now_value, statuses=ACTIVE_CONTRACT_STATUSES | {ContractStatus.DRAFT})
        results: list[ContractRecord] = []
        for contract in expiring:
            if contract.is_terminal():
                continue
            updated = replace(contract, status=ContractStatus.EXPIRED, version=contract.version + 1)
            saved = self.store.save_contract(updated)
            results.append(
                self._log_transition(
                    saved,
                    event_type=ContractEventType.EXPIRED,
                    actor_id=contract.arbiter_id,
                    actor_role="arbiter",
                    from_status=contract.status.value,
                    to_status=saved.status.value,
                    payload={"expired_at": now_value.isoformat()},
                    event_time=now_value,
                )
            )
        return results

    def contract_is_valid(self, contract_id: str, *, now: datetime | None = None) -> bool:
        contract = self._require_contract(contract_id)
        now_value = _ensure_utc(now or datetime.now(UTC))
        if contract.status in TERMINAL_CONTRACT_STATUSES:
            return False
        if contract.sign_a is None or contract.sign_b is None:
            return False
        return contract.is_effective(now=now_value)

    def _require_contract(self, contract_id: str) -> ContractRecord:
        contract = self.store.get_contract(contract_id)
        if contract is None:
            raise ContractValidationError(f"unknown contract_id: {contract_id}")
        return contract

    def _assert_status(self, contract: ContractRecord, allowed: set[ContractStatus]) -> None:
        if contract.status not in allowed:
            allowed_text = ", ".join(sorted(item.value for item in allowed))
            raise ContractValidationError(f"invalid status transition from {contract.status.value}; expected one of {allowed_text}")

    def _ensure_within_signing_window(self, contract: ContractRecord, *, now: datetime | None = None) -> datetime:
        now_value = _ensure_utc(now or datetime.now(UTC))
        if now_value > contract.expires_at:
            raise ContractValidationError("contract signature window has expired")
        return now_value

    def _ensure_within_execution_window(self, contract: ContractRecord, *, now: datetime | None = None) -> datetime:
        now_value = _ensure_utc(now or datetime.now(UTC))
        if contract.effective_until is not None and now_value > contract.effective_until:
            raise ContractValidationError("contract execution window has expired")
        return now_value

    def _log_transition(
        self,
        contract: ContractRecord,
        *,
        event_type: ContractEventType,
        actor_id: str,
        actor_role: str,
        from_status: str,
        to_status: str,
        payload: dict[str, Any],
        event_time: datetime,
    ) -> ContractRecord:
        prev_hash = contract.audit_chain_hash
        event = ContractEvent(
            event_id=f"evt-{uuid4().hex}",
            contract_id=contract.contract_id,
            event_type=event_type,
            actor_id=str(actor_id).strip(),
            actor_role=str(actor_role).strip(),
            from_status=from_status,
            to_status=to_status,
            payload=dict(payload),
            created_at=_ensure_utc(event_time),
            prev_event_hash=prev_hash,
            event_hash=_event_hash(
                contract_id=contract.contract_id,
                event_type=event_type,
                actor_id=str(actor_id).strip(),
                actor_role=str(actor_role).strip(),
                from_status=from_status,
                to_status=to_status,
                payload=dict(payload),
                created_at=_ensure_utc(event_time),
                prev_event_hash=prev_hash,
            ),
        )
        self.store.append_event(event)
        updated = replace(contract, audit_chain_hash=event.event_hash, last_event_at=event.created_at)
        return self.store.save_contract(updated)
