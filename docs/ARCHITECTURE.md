# Architecture

## System Context

SourceVCode Orchestrator is the coordination layer for this workspace. It routes tasks, runs the core AI bridge, manages model and provider policy, and exposes the runtime services used by the rest of the stack.

## High-Level Component Map

1. AI Orchestration Layer
- `core/` decomposes root tasks into DAG-like atomic tasks.
- Routing is capability-driven; model selection is risk/complexity-aware.
- Quality gates, feedback loops, and result merging drive final output quality.
- For current model/provider routing, see [AI Bridge Runtime Routing](./AI_BRIDGE_RUNTIME_ROUTING.md).
- For deeper technical background, see [AI Orchestrator: Core Technical Reference](./AI_ORCHESTRATOR_CORE.md).

2. Data and State Layer
- Managed by `core/db/`.

3. Edge and Observability Layer
- Managed via telemetry and audit logging in `core/`.

## Core Runtime Flows

### AI Orchestration Flow
1. Root task enters orchestrator.
2. Task is decomposed into atomic tasks with dependencies.
3. Model selector and scheduler pick route and execution profile.
4. Agent executes task; quality analyzer verifies output.
5. Feedback loop triggers fix tasks if quality thresholds are not met.
6. Result merger combines outputs into final response.

## Cross-Cutting Concerns

### Security
- Action-level security in the orchestration layer.
- Request-level telemetry for diagnosis and traceability.

### Auditability
- Structured audit trails for orchestrator actions.
- Request-level telemetry for diagnosis and traceability.

### Reliability
- Health checks for orchestrator services.
- Container-level restart policies.

## Source of Truth

- AI runtime behavior: `core/core/*`, `core/agents/*`
- Orchestrator configuration: `core/CONFIG.example.yaml`
- Infrastructure: `docker-compose.ai.yml`

