from __future__ import annotations

from core.core.reasoning_protocol import OrchestratorReasoningProcessor, ReasoningKPI, ReasoningStreamAdapter, ReasoningWSFrame


def test_reasoning_ws_frame_contract_and_efficiency_formula():
    frame = ReasoningWSFrame.model_validate(
        {
            "event": "thinking_chunk",
            "payload": {
                "text": "Inspecting VFS state",
                "metadata": {
                    "active_models": ["deepseek-r1"],
                    "kpi": {
                        "total_time_sec": 12,
                        "thinking_tokens": 6,
                        "agent_count": 2,
                        "efficiency": 1.0,
                    },
                },
            },
        }
    )

    assert frame.event.value == "thinking_chunk"
    assert frame.payload.metadata.active_models == ["deepseek-r1"]
    assert frame.payload.metadata.kpi.efficiency == 1.0


def test_reasoning_kpi_computes_efficiency_when_missing():
    kpi = ReasoningKPI.from_metrics({"total_time_sec": 12, "thinking_tokens": 6, "agent_count": 2})

    assert kpi.efficiency == 1.0


def test_processor_correctly_parses_thinking_and_kpi():
    processor = OrchestratorReasoningProcessor()
    raw_chunk = {
        "event": "thinking_chunk",
        "text": "Анализирую базу данных VFS...",
        "metrics": {"tokens_per_sec": 85, "active_models": ["deepseek-r1"]},
    }

    processed = processor.process(raw_chunk)

    assert processed["is_thinking"] is True
    assert "VFS" in processed["formatted_text"]
    assert processed["ui_metadata"]["speed"] == "85 T/s"
    assert processed["ui_metadata"]["models"] == ["deepseek-r1"]


def test_stream_adapter_classifies_metadata_updates_and_vfs_mentions():
    adapter = ReasoningStreamAdapter()

    frame = adapter.from_console_event("EXECUTION", "task_id=1 agent=local-llm-1 touched VFS cache")

    assert frame["event"] == "metadata_update"
    assert frame["payload"]["metadata"]["vfs_accesses"] == ["task_id=1 agent=local-llm-1 touched VFS cache"]
