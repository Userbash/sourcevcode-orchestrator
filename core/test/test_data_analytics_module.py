import asyncio
from types import SimpleNamespace
from unittest.mock import MagicMock

from core.core.data_analytics_module import DataAnalyticsModule
from core.core.runtime_event_stream_hub import RuntimeEventStreamHub


class _Task:
    def __init__(self) -> None:
        self.routing_hints = {}


def test_data_analytics_module_refreshes_on_load(monkeypatch):
    module = DataAnalyticsModule()
    api = MagicMock()
    api.emit_event = MagicMock()
    api.log = MagicMock()
    api.runtime_event_stream_hub = RuntimeEventStreamHub()

    report = MagicMock()
    report.generated_at = "2026-07-11T05:13:26+00:00"
    report.storage_root = "/tmp/memory_store"
    report.operational_signals = {
        "orchestrator_confidence": "medium",
        "freshness_status": "warning",
        "retrieval_readiness": "limited",
        "analytics_signal_score": 0.63,
        "routing_policy": {
            "policy_mode": "cautious",
            "memory_retrieval_mode": "limited",
            "search_enabled": True,
            "retrieval_enabled": False,
            "prefer_fresh_context": True,
            "degraded_reasons": [],
        },
    }
    report.as_dict.return_value = {
        "generated_at": report.generated_at,
        "storage_root": report.storage_root,
        "source": "file_fallback",
        "management_summary": {"analytics_ready": True},
        "audit": {"issues": []},
        "operational_signals": report.operational_signals,
        "analytics_dimensions": ["created_at"],
        "recommendations": ["keep refreshing"],
        "priorities": ["publish runtime state"],
    }

    writer_calls = []
    monkeypatch.setattr("core.core.data_analytics_module.build_data_storage_analytics_report", lambda: report)
    monkeypatch.setattr("core.core.data_analytics_module.write_data_storage_analytics_report", lambda payload, output_path: writer_calls.append((payload, output_path)))

    asyncio.run(module.on_load(api))
    state = module.finalize()
    runtime_row = api.runtime_event_stream_hub.agent_snapshot("system:data_analytics")

    assert state["status"] == "active"
    assert state["last_refresh_at"] == "2026-07-11T05:13:26+00:00"
    assert state["operational_signals"]["freshness_status"] == "warning"
    assert runtime_row["policy_mode"] == "cautious"
    assert runtime_row["analytics_signal_score"] == 0.63
    assert writer_calls


def test_data_analytics_module_after_task_throttles(monkeypatch):
    module = DataAnalyticsModule()
    api = MagicMock()
    api.emit_event = MagicMock()
    api.log = MagicMock()

    report = MagicMock()
    report.generated_at = "2026-07-11T05:13:26+00:00"
    report.storage_root = "/tmp/memory_store"
    report.operational_signals = {
        "orchestrator_confidence": "high",
        "freshness_status": "healthy",
        "retrieval_readiness": "high",
        "analytics_signal_score": 0.92,
        "routing_policy": {
            "policy_mode": "full",
            "memory_retrieval_mode": "full",
            "search_enabled": True,
            "retrieval_enabled": True,
            "prefer_fresh_context": False,
            "degraded_reasons": [],
        },
    }
    report.as_dict.return_value = {
        "generated_at": report.generated_at,
        "storage_root": report.storage_root,
        "source": "file_fallback",
        "management_summary": {"analytics_ready": True},
        "audit": {"issues": []},
        "operational_signals": report.operational_signals,
        "analytics_dimensions": [],
        "recommendations": [],
        "priorities": [],
    }
    calls = {"count": 0}

    def _build():
        calls["count"] += 1
        return report

    monkeypatch.setattr("core.core.data_analytics_module.build_data_storage_analytics_report", _build)
    monkeypatch.setattr("core.core.data_analytics_module.write_data_storage_analytics_report", lambda payload, output_path: None)
    monkeypatch.setenv("AI_BRIDGE_DATA_ANALYTICS_REFRESH_SEC", "300")

    asyncio.run(module.on_load(api))
    module.after_task(task=None, result=None, context={})

    assert calls["count"] == 1


def test_data_analytics_module_before_task_injects_routing_hints():
    module = DataAnalyticsModule()
    module._last_report = {
        "generated_at": "2026-07-11T05:13:26+00:00",
        "source": "file_fallback",
        "audit": {"issues": ["stale_data_window"]},
        "operational_signals": {
            "freshness_status": "stale",
            "retention_status": "short",
            "session_coverage_status": "limited",
            "search_readiness": "medium",
            "retrieval_readiness": "limited",
            "orchestrator_confidence": "medium",
            "analytics_ready": True,
            "analytics_signal_score": 0.51,
            "routing_policy": {
                "policy_mode": "cautious",
                "memory_retrieval_mode": "limited",
                "search_enabled": True,
                "retrieval_enabled": False,
                "prefer_fresh_context": True,
                "degraded_reasons": ["stale_data_window"],
            },
        },
    }
    task = _Task()
    context = {}

    module.before_task(task, context)

    assert task.routing_hints["data_analytics"]["freshness_status"] == "stale"
    assert task.routing_hints["memory_retrieval_mode"] == "limited"
    assert task.routing_hints["prefer_fresh_context"] is True
    assert context["data_analytics_policy"]["policy_mode"] == "cautious"
