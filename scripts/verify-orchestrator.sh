#!/bin/sh
set -eu

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname "$0")" && pwd)
REPO_ROOT=$(CDPATH= cd -- "$SCRIPT_DIR/.." && pwd)

export GOMODCACHE="${GOMODCACHE:-$REPO_ROOT/.gocache/mod}"
export GOCACHE="${GOCACHE:-$REPO_ROOT/.gocache/build}"

mkdir -p "$GOMODCACHE" "$GOCACHE"

cd "$REPO_ROOT/go-core"
go run ./cmd/verify-orchestrator "$@"
