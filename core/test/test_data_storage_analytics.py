from __future__ import annotations

import json
from pathlib import Path

from core.core import data_storage_analytics as dsa


def _write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


def test_build_data_storage_analytics_report_from_file_store(monkeypatch, tmp_path):
    store = tmp_path / "memory_store"
    _write_json(
        store / "memory_index.json",
        [
            {
                "memory_id": 1,
                "created_at": "2026-06-24T05:28:44.182805+00:00",
                "updated_at": "2026-06-24T05:40:00+00:00",
            },
            {
                "memory_id": 2,
                "created_at": "2026-07-11T05:13:26.866260+00:00",
                "updated_at": "2026-07-11T05:20:00+00:00",
            },
        ],
    )
    _write_json(
        store / "trained_memory_index.json",
        [
            {
                "trained_memory_id": 1,
                "created_at": "2026-06-24T05:28:44.655054+00:00",
                "updated_at": "2026-07-08T09:02:01.828253+00:00",
            }
        ],
    )
    _write_json(store / "session_map.json", {"session-1": "normalized-1", "session-2": "normalized-2"})

    (store / "memories").mkdir(parents=True)
    (store / "memories" / "1.json").write_text('{"content":"a"}', encoding="utf-8")
    (store / "memories" / "2.json").write_text('{"content":"b"}', encoding="utf-8")
    (store / "commands").mkdir(parents=True)
    (store / "commands" / "1.json").write_text('{"command":"ls"}', encoding="utf-8")
    (store / "vfs").mkdir(parents=True)
    (store / "vfs" / "file.txt").write_text("hello", encoding="utf-8")
    (store / "training").mkdir(parents=True)
    (store / "training" / "dataset.jsonl").write_text('{"x":1}\n', encoding="utf-8")

    monkeypatch.setattr(
        dsa,
        "snapshot_postgres_data_plane",
        lambda database_url: type(
            "Snapshot",
            (),
            {
                "as_dict": lambda self: {
                    "ok": False,
                    "postgres_state": "missing",
                    "tables": [],
                    "details": "database url not configured",
                }
            },
        )(),
    )

    report = dsa.build_data_storage_analytics_report(database_url=None, storage_dir=store)

    assert report.source == "file_fallback"
    assert report.sessions_tracked == 2
    assert report.management_summary["earliest_data_at"] == "2026-06-24T05:28:44.182805+00:00"
    assert report.management_summary["latest_data_at"] == "2026-07-11T05:13:26.866260+00:00"
    assert report.management_summary["retention_days"] == 16.989
    assert report.audit["latest_data_at"] == "2026-07-11T05:13:26.866260+00:00"
    assert report.operational_signals["retrieval_readiness"] in {"low", "limited", "medium", "high"}
    assert 0.0 <= report.operational_signals["analytics_signal_score"] <= 1.0
    assert report.operational_signals["routing_policy"]["policy_mode"] in {"full", "cautious", "degraded"}
    assert "session_id" in report.analytics_dimensions
    assert "provider" in report.analytics_dimensions
    assert "token_usage" in report.analytics_dimensions

    memories = next(item for item in report.datasets if item.name == "memories")
    trained = next(item for item in report.datasets if item.name == "trained_memories")

    assert memories.rows == 2
    assert memories.file_count == 2
    assert memories.created_range.first_id == 1
    assert memories.created_range.last_id == 2
    assert memories.created_range.retention_days == 16.989
    assert trained.rows == 1
    assert "Retention window is under 30 days." in report.recommendations[0]
    assert any("Store a daily analytics snapshot" in item for item in report.priorities)
    assert any("runtime WS stream" in item for item in report.priorities)


def test_write_data_storage_analytics_report(tmp_path):
    report = dsa.DataStorageAnalyticsReport(
        generated_at="2026-07-11T05:13:26+00:00",
        source="file_fallback",
        database_url_configured=False,
        storage_root=str(tmp_path / "memory_store"),
        total_size_bytes=128,
        total_size_mb=0.0,
        total_files=2,
        sessions_tracked=1,
    )
    output_path = tmp_path / "out" / "report.json"

    dsa.write_data_storage_analytics_report(report, output_path=output_path)

    payload = json.loads(output_path.read_text(encoding="utf-8"))
    assert payload["source"] == "file_fallback"
    assert payload["total_size_bytes"] == 128
