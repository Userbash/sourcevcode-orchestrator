# Operations, Runbooks, and Plans

## 1. Operations Runbook
- **Standard Startup:**
    - Validate `.env`.
    - `bash scripts/start_core_stack.sh`.
    - Health: `GET /health` (`http://localhost:8000/health`).
- **Standard Shutdown:** Graceful orchestrator termination.
- **Routine Checks:** Orchestrator health, error rates, latency, AI connectivity.
- **Change Deployment:** Changelog review, tests, smoke checks, manifest recording.

## 2. Incident Response Runbook
- **Severity Model:** SEV-1 (outage) to SEV-3 (degradation).
- **Workflow:** Triage -> Evidence Capture (logs/metrics) -> Mitigation -> Communication -> Postmortem.

## 3. Orchestration & Hardening Plans
- **Hardening Backlog:** Micro-tasks for runtime isolation, observability, reliability, governance.
- **Execution Plan (R1-R4):** Encapsulated rollout waves (R1: Critical Hardening; R2: Secure Delivery; R3: Governance; R4: Architecture).
- **Core Integrity Plan:** Micro-tasks for testing, validation, and health checks.
- **Memory Upgrade Plan:** Vector storage, hybrid retrieval, scoring/decay lifecycle.

## 4. Session Memory & VFS
- **Goal:** Shared, validated, resilient execution context.
- **Backend:** PostgreSQL (Tables: `memories`, `json_themes`, `vfs_files`).
- **Scopes:** session, task, agent, capability.
- **Security:** Redaction before write, max size policy.

## 5. Routing Rules
- **Complexity-based Routing:** Low (local/fast), Medium (Mistral), High/Critical (OpenAI/fallback).
- **Antigravity CLI:** Routes to `agy`.
- **OpenAI:** Auto-routing with session token budget tracking.
