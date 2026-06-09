# System Overview & Architecture

## 1. Project Context
SourceVCode Orchestrator is the coordination layer for this workspace. It routes tasks, runs the core AI bridge, manages model and provider policy, and exposes the runtime services used by the rest of the stack.

## 2. High-Level Component Map
1. **AI Orchestration Layer**
   - `core/` decomposes root tasks into DAG-like atomic tasks.
   - Routing is capability-driven; model selection is risk/complexity-aware.
   - Quality gates, feedback loops, and result merging drive final output quality.

2. **Data and State Layer**
   - Managed by `core/db/`.

3. **Edge and Observability Layer**
   - Managed via telemetry and audit logging in `core/`.

## 3. Core Runtime Flows

### AI Orchestration Flow
1. Root task enters orchestrator.
2. Task is decomposed into atomic tasks with dependencies.
3. Model selector and scheduler pick route and execution profile.
4. Agent executes task; quality analyzer verifies output.
5. Feedback loop triggers fix tasks if quality thresholds are not met.
6. Result merger combines outputs into final response.

## 4. Technical Background & AI Bridge
*Content migrated from legacy architecture documentation.*

### SecurityManager (`core/security.py`)
- validates shell commands against a strict allowlist;
- redacts API keys, tokens, passwords, and similar secrets;
- trims sensitive fields before sending data to external AI providers.

### LoadBalancer (`core/load_balancer.py`)
- weighs success rate, availability, latency, cost, and specialization;
- applies an overload penalty when an agent is already too busy.

### TaskDecomposer (`core/task_decomposer.py`)
- maps high-level work to the right task type;
- adds the capability and model hints needed by the router;
- keeps dependencies explicit so the DAG stays readable.

### FeedbackLoop (`core/feedback_loop.py`)
- tracks retry counts per task;
- creates a `FIX` task when the current result does not pass review;
- pushes the fix back through the router instead of patching in place.

### AuditTrail (`core/audit.py`)
- writes immutable execution records;
- keeps raw input and secrets out of the log stream;
- records the decisions that matter for later diagnosis.
