from __future__ import annotations

from datetime import UTC, datetime
from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class ReasoningEvent(str, Enum):
    THINKING_START = "thinking_start"
    THINKING_CHUNK = "thinking_chunk"
    THINKING_END = "thinking_end"
    ANSWER_CHUNK = "answer_chunk"
    METADATA_UPDATE = "metadata_update"


class ReasoningKPI(BaseModel):
    model_config = ConfigDict(extra="allow")

    tokens_per_sec: float | None = None
    latency_ms: float | None = None
    total_time_sec: float | None = None
    thinking_tokens: int = 0
    agent_count: int = 0
    efficiency: float | None = None

    @classmethod
    def from_metrics(cls, metrics: dict[str, Any] | None = None) -> "ReasoningKPI":
        payload = dict(metrics or {})
        thinking_tokens = int(payload.get("thinking_tokens") or payload.get("thinking_length_tokens") or 0)
        agent_count = int(payload.get("agent_count") or payload.get("active_agents") or 0)
        total_time_sec = payload.get("total_time_sec")
        latency_ms = payload.get("latency_ms")
        if total_time_sec is None and latency_ms is not None:
            try:
                total_time_sec = float(latency_ms) / 1000.0
            except (TypeError, ValueError):
                total_time_sec = None
        efficiency = payload.get("efficiency")
        if efficiency is None and total_time_sec is not None and thinking_tokens > 0 and agent_count > 0:
            efficiency = float(total_time_sec) / float(thinking_tokens * agent_count)
        payload.update(
            {
                "thinking_tokens": thinking_tokens,
                "agent_count": agent_count,
                "total_time_sec": total_time_sec,
                "efficiency": efficiency,
            }
        )
        return cls.model_validate(payload)


class ReasoningMetadata(BaseModel):
    model_config = ConfigDict(extra="allow")

    active_models: list[str] = Field(default_factory=list)
    memory_status: dict[str, Any] = Field(default_factory=dict)
    kpi: ReasoningKPI = Field(default_factory=ReasoningKPI)
    vfs_accesses: list[str] = Field(default_factory=list)
    training_weights: dict[str, float] = Field(default_factory=dict)
    stage: str | None = None
    status: str | None = None


class ReasoningPayload(BaseModel):
    model_config = ConfigDict(extra="allow")

    text: str = ""
    metadata: ReasoningMetadata = Field(default_factory=ReasoningMetadata)


class ReasoningWSFrame(BaseModel):
    model_config = ConfigDict(extra="allow")

    event: ReasoningEvent
    payload: ReasoningPayload
    ts: str = Field(default_factory=lambda: datetime.now(UTC).isoformat())

    def as_ws_event(self) -> dict[str, Any]:
        data = self.model_dump(mode="json")
        return {"type": "protocol_event", **data}


class OrchestratorReasoningProcessor:
    def process(self, chunk: dict[str, Any]) -> dict[str, Any]:
        metrics = dict(chunk.get("metrics") or {})
        active_models = [str(item) for item in (metrics.get("active_models") or []) if str(item).strip()]
        metadata = ReasoningMetadata(
            active_models=active_models,
            kpi=ReasoningKPI.from_metrics(metrics),
            stage=str(chunk.get("event") or "").upper() or None,
        )
        return {
            "is_thinking": chunk.get("event") == ReasoningEvent.THINKING_CHUNK.value,
            "formatted_text": f"[Рассуждение] {chunk.get('text', '')}".strip(),
            "ui_metadata": {
                "speed": f"{int(metrics.get('tokens_per_sec', 0))} T/s" if metrics.get("tokens_per_sec") is not None else "0 T/s",
                "models": metadata.active_models,
                "efficiency": metadata.kpi.efficiency,
            },
        }


class ReasoningStreamAdapter:
    _METADATA_STAGES = {
        "AGENTS",
        "EXECUTION",
        "FALLBACK",
        "MODEL_SELECTION",
        "PARALLEL_ROUTE",
        "P2P_HANDOFF",
        "ROUTING",
        "SCHEDULER",
        "THROTTLE",
        "TOKEN_BUDGET",
    }

    def __init__(self) -> None:
        self._started_at = datetime.now(UTC)
        self._thinking_fragments: list[str] = []

    @staticmethod
    def _memory_status(stage: str, message: str) -> dict[str, Any]:
        lowered = f"{stage} {message}".lower()
        return {
            "mentioned": any(token in lowered for token in ("memory", "postgres", "rabbitmq", "session")),
            "stage": stage,
        }

    @staticmethod
    def _vfs_accesses(message: str) -> list[str]:
        return [message] if "vfs" in message.lower() else []

    def _frame(self, event: ReasoningEvent, *, text: str = "", stage: str | None = None, status: str | None = None, active_models: list[str] | None = None, total_time_sec: float | None = None, thinking_tokens: int | None = None, agent_count: int | None = None, extra: dict[str, Any] | None = None) -> dict[str, Any]:
        metadata = ReasoningMetadata(
            active_models=list(active_models or []),
            memory_status=self._memory_status(stage or "", text),
            vfs_accesses=self._vfs_accesses(text),
            kpi=ReasoningKPI.from_metrics(
                {
                    "total_time_sec": total_time_sec,
                    "thinking_tokens": thinking_tokens,
                    "agent_count": agent_count,
                    **dict(extra or {}),
                }
            ),
            stage=stage,
            status=status,
        )
        payload = ReasoningPayload(text=text, metadata=metadata)
        return ReasoningWSFrame(event=event, payload=payload).as_ws_event()

    def accepted(self) -> dict[str, Any]:
        return self._frame(ReasoningEvent.THINKING_START, stage="ACCEPTED", text="task accepted by orchestrator")

    def from_console_event(self, stage: str, message: str) -> dict[str, Any]:
        cleaned_stage = str(stage or "").strip().upper()
        cleaned_message = str(message or "")
        if cleaned_message:
            self._thinking_fragments.append(cleaned_message)
        event = ReasoningEvent.METADATA_UPDATE if cleaned_stage in self._METADATA_STAGES else ReasoningEvent.THINKING_CHUNK
        return self._frame(event, stage=cleaned_stage, text=cleaned_message)

    def finished(self, result: dict[str, Any] | None = None, *, status: str = "done") -> dict[str, Any]:
        now = datetime.now(UTC)
        elapsed = (now - self._started_at).total_seconds()
        thinking_tokens = sum(len(fragment.split()) for fragment in self._thinking_fragments)
        result_payload = dict(result or {})
        agent_count = len(result_payload.get("results") or []) or len(result_payload.get("metrics", {}).get("agents", []) or [])
        return self._frame(
            ReasoningEvent.THINKING_END,
            stage="DONE" if status == "done" else "ERROR",
            status=status,
            total_time_sec=elapsed,
            thinking_tokens=thinking_tokens,
            agent_count=agent_count,
        )

    def answer(self, result: dict[str, Any] | None = None) -> dict[str, Any] | None:
        payload = dict(result or {})
        merged = payload.get("merged") if isinstance(payload.get("merged"), dict) else {}
        text = str(merged.get("summary") or payload.get("summary") or "").strip()
        if not text:
            return None
        return self._frame(ReasoningEvent.ANSWER_CHUNK, stage="ANSWER", text=text)
