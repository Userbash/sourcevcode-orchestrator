from __future__ import annotations

import threading

from core.core.orchestrator import Orchestrator


def build_http_app(orchestrator: Orchestrator):
    """Temporary facade over the legacy app factory during migration."""
    from core.scripts.orchestrator_daemon import _build_http_app

    return _build_http_app(orchestrator)


def start_http_server(orchestrator: Orchestrator) -> None:
    from core.scripts.orchestrator_daemon import _assert_required_http_routes, _resolve_http_port
    import uvicorn

    app = build_http_app(orchestrator)
    _assert_required_http_routes(app)
    port = _resolve_http_port()
    config = uvicorn.Config(app, host="0.0.0.0", port=port, log_level="warning")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
