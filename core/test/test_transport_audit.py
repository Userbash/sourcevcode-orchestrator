from __future__ import annotations

from core.core.transport_audit import build_transport_audit


def test_build_transport_audit_reports_control_ws_as_primary_interactive_plane():
    report = build_transport_audit(None)

    assert report["status"] == "ok"
    assert report["summary"]["control_plane_transport"] == "hybrid_http_ws"
    assert report["summary"]["event_stream_transport"] == "websocket"
    assert "/control/ws" in report["ws_endpoints"]
    assert "/health" in report["http_endpoints"]
    assert any(item["name"] == "control_plane_ws" for item in report["subsystems"])
    assert report["migration_plan"][0]["targets"] == ["/health", "/health/full", "/api/health"]
    assert "sourcecraft.delegate" in report["migration_plan"][1]["targets"]
