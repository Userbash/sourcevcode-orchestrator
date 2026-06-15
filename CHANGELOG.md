# Changelog

All notable changes to this project are documented in this file.

The format follows Keep a Changelog principles and Semantic Versioning (`MAJOR.MINOR.PATCH`).

## [Unreleased]

### Added
- Input normalization helpers for task intake:
  - Unicode cleanup, whitespace compaction, control-character stripping, and list normalization
  - heuristic request quantization for intent, risk, scope, execution shape, quality, and confidence
- Routing-profile propagation across orchestration:
  - normalized intake profile attached to created tasks
  - prompt optimizer context enriched with normalized reasons and execution guidance
  - memory runtime context enriched with normalization guidance for downstream agents
- Regression coverage for normalized payload parsing, provider preference escalation, secure routing, and normalized-profile-based decomposition
- Delivery supervision for local agent execution:
  - tracked delivery records and handshake states
  - payload checksum validation before execution
  - retry and dead-letter handling on ACK timeout
  - delivery telemetry in KPI snapshots and mailbox health views
- Antigravity session persistence and recovery controls:
  - local session state store with last-success and last-failure metadata
  - login cooldown and recent-session grace windows
  - explicit session-control status for user-facing and orchestrator-facing flows
- Memory event publishing:
  - `memory.events` for stored memories and remembered commands
  - `memory.trained.events` for trained memory storage, outcomes, and rejections
- Targeted regression coverage for delivery supervision, memory event emission, and Antigravity session handling
- Documentation governance baseline:
  - architecture map (`docs/ARCHITECTURE.md`)
  - API documentation structure (`docs/API/*`)
  - ADR registry (`docs/ADR/*`)
  - operations runbooks (`docs/RUNBOOKS/*`)
  - versioning policy (`docs/VERSIONING_POLICY.md`)
  - database migration playbook (`docs/DB_MIGRATION_PLAYBOOK.md`)
  - RBAC matrix (`docs/RBAC_MATRIX.md`)
  - security changelog (`docs/SECURITY_CHANGELOG.md`)
  - test coverage map (`docs/TEST_COVERAGE_MAP.md`)
  - release manifest template (`docs/RELEASE_MANIFEST_TEMPLATE.md`)
- CI documentation quality checks:
  - markdown local-link validation
  - API route documentation coverage validation

### Changed
- Task routing, provider prioritization, and decomposition now react to normalized intake risk, quality, and execution-shape signals instead of relying only on raw task complexity.
- High-risk or noisy requests now prefer stronger validation lanes and stronger providers, while multi-file code requests can trigger earlier parallel fan-out.
- Simplified the orchestrator module set by removing legacy frontend, UI-theme, websocket, API bridge, and auto-dev pipeline paths from the active runtime.
- Routed local agent execution through delivery envelopes and mailbox handshakes instead of direct `agent.run(...)` calls.
- Wired session memory and persistent memory to the message bus so memory activity can be observed externally.
- Switched `docker-compose.ai.yml` to an explicit RabbitMQ-backed message bus configuration and added a RabbitMQ healthcheck dependency.
- Trimmed repository documentation to the backend, orchestrator, and infrastructure layers that still exist in this repository.
- Expanded root `README.md` with a documentation index and governance workflow.
- Added root npm scripts for docs verification and route-doc synchronization checks.
- Added pull request template with mandatory risk, migration, rollback, and traceability sections.

### Removed
- Legacy frontend-specific agents and worker scaffolding that are no longer part of the supported release path.
- Deprecated modules and tests for frontend generation, websocket protocol variants, JSON theme storage, API bridge, image orchestration, and related one-shot task normalization.

### Fixed
- Antigravity authorization recovery now distinguishes auth failures from transient runtime faults and avoids repeated relogin loops.
- KPI summaries now include delivery backlog, retries, dead letters, and live queue health by agent.
- Data-plane and storage schema handling no longer reference removed `json_themes` structures.

## [2.0.0] - 2026-05-24

### Added
- Core backend API services and deployment hardening.
- Extended RBAC, audit, telemetry, and security guardrails.
- AI Bridge orchestration protocol updates and routing stabilization.
- Admin panel and observability improvements.

### Fixed
- Registration security edge cases (disposable domains, CORS origin handling).
- Admin telemetry stability and limiter behavior.

