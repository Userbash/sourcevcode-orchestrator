# Changelog

## Unreleased

### Documentation and repository structure

- Moved project documentation to the repository root and consolidated detailed technical material under `docs/`.
- Rewrote the root `README.md` as the primary publication entrypoint for architecture, runtime behavior, transport, and deployment.
- Added dedicated documentation for architecture, API and transport, runtime routing and model selection, and deployment and publication.
- Removed duplicated documentation from `go-core` so the repository root is the single source of truth.

### Runtime routing and model selection

- Fixed the provider mismatch between model selection and agent routing so `AssignedProvider` is preserved during routing.
- Updated fallback handling to keep provider and model rebinding consistent during reroute scenarios.
- Expanded model selection logic to use live inventory, complexity, risk, route history, retrieval signals, memory, and budget pressure.
- Improved runtime model inventory handling to distinguish ready, degraded, unavailable, missing, and not-configured model states.
- Added tests that cover provider-safe routing and model selection behavior.

### API and transport

- Added transport audit support for WebSocket traffic and orchestration envelope tracing.
- Exposed runtime payload and control surface updates needed for inventory and routing introspection.
- Added relay and audit bridge scripts for interactive WebSocket validation and transport inspection.
- Added an external chat proxy service that forwards external chat traffic to the internal orchestrator and keeps the orchestrator authoritative.

### Packaging and deployment

- Updated the orchestrator container packaging.
- Added a root `docker-compose.yml` that defines the local orchestration stack and external chat gateway.
- Rebuilt and validated the runtime container against the live `127.0.0.1:8010` health and WebSocket path.
