# Documentation Index

This directory now documents the current orchestrator runtime first.

Older documents are still present when they contain useful release history or migration context, but they should not be read as the primary description of the live system. If two documents disagree, prefer the files listed under **Current Runtime**.

## Current Runtime

- `SYSTEM_OVERVIEW.md` - the best starting point for understanding how the orchestrator works today
- `RUNTIME_CHANGES_AND_MIGRATION_NOTES.md` - what changed compared with older documentation and older runtime slices
- `AI_BRIDGE_RUNTIME_ROUTING.md` - provider and model routing policy
- `AI_BRIDGE_ARCHITECTURE.md` - bridge-level component wiring
- `AI_ORCHESTRATOR_CORE.md` - technical reference for core orchestration behavior
- `API/README.md` - API documentation structure
- `API/openapi.yaml` - OpenAPI contract

## Operations

- `RUNBOOKS/OPERATIONS_RUNBOOK.md` - deployment and recovery procedures
- `TEST_COVERAGE_MAP.md` - test coverage and known gaps
- `AI_BRIDGE_HARDENING_BACKLOG.md` - runtime hardening backlog

## Memory and Validation

- `AI_BRIDGE_SESSION_MEMORY.md` - session and layered memory design
- `RELEASE_SUMMARY_LAYERED_RUNTIME_MEMORY_AND_MULTI_AGENT_ORCHESTRATION.md` - release summary for memory, decomposition, and multi-agent routing changes

## Governance

- `DOCUMENTATION_GOVERNANCE.md` - documentation update rules
- `VERSIONING_POLICY.md` - versioning policy
- `TRACEABILITY_POLICY.md` - traceability rules
- `ENVIRONMENT_VERSIONING.md` - reproducible environment rules

## Historical and Reference Material

These files are still useful, but they describe broader or older slices of the platform:

- `PROJECT_FULL_DOCUMENTATION_EN.md`
- `SOURCECRAFT_PUBLICATION_REPORT.md`
- `LLM_COST_CACHE_BLUEPRINT.md`

When you update the runtime, update `SYSTEM_OVERVIEW.md` and `RUNTIME_CHANGES_AND_MIGRATION_NOTES.md` first. That keeps the high-level story accurate and makes the rest of the documentation easier to trust.
