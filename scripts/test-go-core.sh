#!/bin/sh
set -eu

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname "$0")" && pwd)
REPO_ROOT=$(CDPATH= cd -- "$SCRIPT_DIR/.." && pwd)
GO_CORE_DIR="$REPO_ROOT/go-core"
ARTIFACT_DIR="${ARTIFACT_DIR:-$REPO_ROOT/.artifacts}"
COVERAGE_FILE="${COVERAGE_FILE:-$ARTIFACT_DIR/go-core.coverage.out}"
MIN_COVERAGE="${MIN_COVERAGE:-0}"

export GOMODCACHE="${GOMODCACHE:-$REPO_ROOT/.gocache/mod}"
export GOCACHE="${GOCACHE:-$REPO_ROOT/.gocache/build}"

mkdir -p "$GOMODCACHE" "$GOCACHE" "$ARTIFACT_DIR"

cd "$GO_CORE_DIR"

printf '%s
' '[1/6] full unit test sweep with coverage'
go test -covermode=atomic -coverprofile="$COVERAGE_FILE" ./...

printf '%s
' '[2/6] websocket and delivery smoke tests'
go test ./internal/api -run 'TestControlWebSocketEndToEnd|TestCompatibilityRouteAdvertisesControlWebSocket|TestDeliveryMailboxLifecycleUsesGoRuntime' -count=1

printf '%s
' '[3/6] execution-plan end-to-end tests'
go test ./internal/kernel -run 'TestRunExecutionPlanExecutesParallelBranchesConcurrently|TestPreviewExecutionPlanRealCodeTaskBuildsParallelBranches|TestRunExecutionPlanRealTaskCollectsWorkflowEvidence' -count=1

printf '%s
' '[4/6] verifier regression tests'
go test ./cmd/verify-orchestrator -run 'TestRunSyntheticRuntimeProfileCapturesAgentKPIs' -count=1

printf '%s
' '[5/6] kernel race tests'
go test -race ./internal/kernel -count=1

printf '%s
' '[6/6] orchestrator verification pipeline'
go run ./cmd/verify-orchestrator -json -skip-race

coverage_line=$(go tool cover -func="$COVERAGE_FILE" | tail -n 1)
coverage_value=$(printf '%s' "$coverage_line" | awk '{print $3}' | tr -d '%')
printf 'coverage summary: %s
' "$coverage_line"
awk -v actual="$coverage_value" -v min="$MIN_COVERAGE" 'BEGIN { exit !(actual + 0 >= min + 0) }' || {
  printf 'coverage gate failed: actual=%s%% min=%s%%
' "$coverage_value" "$MIN_COVERAGE" >&2
  exit 1
}
