from __future__ import annotations

from copy import deepcopy
from dataclasses import asdict, is_dataclass
from datetime import UTC, datetime
from typing import Any, Mapping

from core.core.contracts import ContractEvent as DomainContractEvent
from core.core.contracts import ContractRecord, ContractStatus


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _coerce_datetime(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=UTC)
        return value.astimezone(UTC)
    if isinstance(value, str):
        normalized = value[:-1] + "+00:00" if value.endswith("Z") else value
        parsed = datetime.fromisoformat(normalized)
        if parsed.tzinfo is None:
            return parsed.replace(tzinfo=UTC)
        return parsed.astimezone(UTC)
    raise TypeError(f"unsupported datetime value: {type(value).__name__}")


class InMemoryContractsStorage:
    def __init__(self) -> None:
        self._contracts: dict[str, dict[str, Any]] = {}
        self._contract_models: dict[str, ContractRecord] = {}
        self._events: dict[str, list[dict[str, Any]]] = {}
        self._event_models: dict[str, list[DomainContractEvent]] = {}

    def save(self, contract_id: str, contract: Mapping[str, Any]) -> dict[str, Any]:
        snapshot = deepcopy(dict(contract))
        snapshot["contract_id"] = str(contract_id)
        self._contracts[str(contract_id)] = snapshot
        return deepcopy(snapshot)

    def save_contract(self, contract: ContractRecord) -> ContractRecord:
        contract_id = str(contract.contract_id)
        self._contract_models[contract_id] = deepcopy(contract)
        self._contracts[contract_id] = _contract_to_snapshot(contract)
        return deepcopy(contract)

    def get(self, contract_id: str) -> dict[str, Any] | None:
        snapshot = self._contracts.get(str(contract_id))
        return deepcopy(snapshot) if snapshot is not None else None

    def get_contract(self, contract_id: str) -> ContractRecord | None:
        contract = self._contract_models.get(str(contract_id))
        return deepcopy(contract) if contract is not None else None

    def list_by_status(self, statuses: str | set[ContractStatus]) -> list[Any]:
        if isinstance(statuses, str):
            matches = [snapshot for snapshot in self._contracts.values() if snapshot.get("status") == statuses]
            return [deepcopy(snapshot) for snapshot in sorted(matches, key=lambda item: str(item["contract_id"]))]

        allowed = {status.value if isinstance(status, ContractStatus) else str(status) for status in statuses}
        matches = [contract for contract in self._contract_models.values() if contract.status.value in allowed]
        return [deepcopy(contract) for contract in sorted(matches, key=lambda item: item.contract_id)]

    def list_expiring(self, before: datetime) -> list[dict[str, Any]]:
        threshold = _coerce_datetime(before)
        assert threshold is not None
        matches: list[dict[str, Any]] = []
        for snapshot in self._contracts.values():
            expires_at = _coerce_datetime(snapshot.get("expires_at"))
            if expires_at is not None and expires_at <= threshold:
                matches.append(snapshot)
        return [
            deepcopy(snapshot)
            for snapshot in sorted(
                matches,
                key=lambda item: (
                    _coerce_datetime(item.get("expires_at")) or datetime.max.replace(tzinfo=UTC),
                    str(item["contract_id"]),
                ),
            )
        ]

    def list_expiring_before(
        self,
        deadline: datetime,
        *,
        statuses: set[ContractStatus] | None = None,
    ) -> list[ContractRecord]:
        threshold = _coerce_datetime(deadline)
        assert threshold is not None
        allowed = None
        if statuses is not None:
            allowed = {status.value if isinstance(status, ContractStatus) else str(status) for status in statuses}

        matches: list[ContractRecord] = []
        for contract in self._contract_models.values():
            if allowed is not None and contract.status.value not in allowed:
                continue
            expiry_marker = contract.effective_until if contract.effective_until is not None else contract.expires_at
            if expiry_marker <= threshold:
                matches.append(contract)
        return [
            deepcopy(contract)
            for contract in sorted(
                matches,
                key=lambda item: (
                    item.effective_until if item.effective_until is not None else item.expires_at,
                    item.contract_id,
                ),
            )
        ]

    def append_event(self, *args: Any, **kwargs: Any) -> Any:
        if args and isinstance(args[0], DomainContractEvent):
            event = deepcopy(args[0])
            contract_id = event.contract_id
            self._event_models.setdefault(contract_id, []).append(event)
            self._events.setdefault(contract_id, []).append(_event_to_snapshot(event))
            return deepcopy(event)

        contract_id = str(args[0])
        event_type = str(args[1])
        payload = deepcopy(dict(args[2] if len(args) > 2 else kwargs.get("payload") or {}))
        logged_at = (_coerce_datetime(kwargs.get("logged_at")) or _utc_now()).isoformat()
        sequence = len(self._events.setdefault(contract_id, [])) + 1
        event = {
            "contract_id": contract_id,
            "event_type": event_type,
            "payload": payload,
            "logged_at": logged_at,
            "sequence": sequence,
        }
        self._events[contract_id].append(deepcopy(event))
        return deepcopy(event)

    def events_for_contract(self, contract_id: str) -> list[dict[str, Any]]:
        return [deepcopy(event) for event in self._events.get(str(contract_id), [])]


def _contract_to_snapshot(contract: ContractRecord) -> dict[str, Any]:
    snapshot = asdict(contract) if is_dataclass(contract) else deepcopy(contract)
    snapshot["status"] = contract.status.value
    snapshot["created_at"] = contract.created_at.isoformat()
    snapshot["expires_at"] = contract.expires_at.isoformat()
    snapshot["effective_from"] = contract.effective_from.isoformat() if contract.effective_from else None
    snapshot["effective_until"] = contract.effective_until.isoformat() if contract.effective_until else None
    snapshot["last_event_at"] = contract.last_event_at.isoformat() if contract.last_event_at else None
    if contract.sign_a is not None:
        snapshot["sign_a"]["signed_at"] = contract.sign_a.signed_at.isoformat()
    if contract.sign_b is not None:
        snapshot["sign_b"]["signed_at"] = contract.sign_b.signed_at.isoformat()
    return snapshot


def _event_to_snapshot(event: DomainContractEvent) -> dict[str, Any]:
    return {
        "event_id": event.event_id,
        "contract_id": event.contract_id,
        "event_type": event.event_type.value,
        "actor_id": event.actor_id,
        "actor_role": event.actor_role,
        "from_status": event.from_status,
        "to_status": event.to_status,
        "payload": deepcopy(event.payload),
        "created_at": event.created_at.isoformat(),
        "prev_event_hash": event.prev_event_hash,
        "event_hash": event.event_hash,
    }
