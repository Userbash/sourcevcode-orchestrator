# Runtime Release Notes: July 16, 2026

## Summary

This release consolidates the operational scripts, introduces versioned `go_core` rollouts, adds runtime build visibility over HTTP and WebSocket, and hardens the orchestrator with new verification and regression coverage.

## Script Layout Consolidation

- Removed the duplicate `script/` tree.
- Moved all helper assets into a single root `scripts/` directory.
- Updated documentation, compose references, and bridge scripts to point at the consolidated paths.

## Versioned `go_core` Images and Rollouts

- `go-core/Dockerfile` now embeds `VERSION`, `COMMIT`, and `BUILD_TIME` into the binary at build time.
- The image publishes the same metadata as OCI labels.
- `scripts/run-podman-stack.sh` now builds versioned images such as `localhost/go-core:<timestamp>-<commit>` and maintains the `localhost/go-core:current` alias.
- The active rollout target is stored in `.runtime/go-core-image.ref` so scripted restarts and compose-based restarts use the same image reference.
- Added `status`, `print-go-core-target`, and `pin-go-core` commands to inspect or override the selected runtime image.

## Runtime Version Visibility

- `/health/full` now returns `kernel_version` with the running `version`, `commit`, and `build_time`.
- `/control/ws` now emits an initial `kernel.version` system frame so internal tooling can validate the live build immediately after connect.
- `go-core/cmd/orchestrator` logs the build metadata on startup and exposes a `version` subcommand.

## Verification and Automated Checks

- Added `go-core/cmd/verify-orchestrator`, which exports a JSON runtime profile for the orchestrator.
- The verifier captures goroutines, heap usage, allocation deltas, GC activity, execution profile data, delivery snapshots, state snapshots, agent KPIs, and coordinator KPIs.
- Added `scripts/verify-orchestrator.sh` for quick JSON export.
- Added `scripts/test-go-core.sh` to run the full automated validation pipeline:
  - `go test ./...` with coverage
  - API smoke tests
  - orchestrator end-to-end tests
  - verifier regression tests
  - `go test -race ./internal/kernel`
  - `go run ./cmd/verify-orchestrator -json -skip-race`

## Correctness Fixes and Regression Coverage

### Planner terminal status validation

- Hardened `advanced_planner` so a plan only succeeds when every workflow reaches a successful terminal state.
- Rejected, failed, or otherwise non-success terminal workflows now fail the plan instead of being treated as completed.
- Added regression tests to keep that behavior locked.

### Delivery supervisor snapshot safety

- Cloned delivery records before refreshing, snapshotting, and reporting health.
- This removes shared-state mutation from observer paths and reduces the chance of race-induced corruption.

### API and bridge test stability

- Updated API tests to force the in-memory message bus during test runs.
- Added WebSocket assertions for the new `kernel.version` bootstrap frame.

## Retrieval-Aware Routing and Memory Enrichment

- Added retrieval-aware memory loading and scoring for router decisions.
- Extended memory context assembly with session, route, and knowledge segments plus retrieval KPI data.
- Added retrieval routing tests and coverage around the new memory enrichment path.

## Model Catalog Synchronization

- Added model catalog loading from configurable catalog files.
- Provider discovery can now synchronize the live model inventory back into the configured catalog file.
- Added catalog tests and fixture files under the API and kernel config trees.

## Operational Outcome

Operators can now answer three runtime questions deterministically:

1. Which exact `go_core` build is live?
2. Are HTTP and WebSocket traffic using the same kernel build?
3. Has the candidate build passed the automated verifier, race checks, and smoke tests before rollout?

The new versioned rollout flow and runtime metadata make those answers available through a single scripted path instead of manual guesswork.
