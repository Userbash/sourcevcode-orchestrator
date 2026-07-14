#!/bin/sh

set -eu

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
ROOT_DIR=$(CDPATH= cd -- "$SCRIPT_DIR/.." && pwd)
GO_CORE_DIR="$ROOT_DIR/go-core"
OUTPUT_BIN="${ORCHESTRATOR_OUTPUT_BIN:-$GO_CORE_DIR/orchestrator}"

mkdir -p "$(dirname -- "$OUTPUT_BIN")"

export GOMODCACHE="${GOMODCACHE:-$ROOT_DIR/.gomodcache}"
export GOCACHE="${GOCACHE:-$ROOT_DIR/.gocache}"

cd "$GO_CORE_DIR"
go build -o "$OUTPUT_BIN" ./cmd/orchestrator

echo "built=$OUTPUT_BIN"
echo "run=$ROOT_DIR/bin/orchestrator-host.sh start"
