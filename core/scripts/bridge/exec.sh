#!/bin/bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/../../.." && pwd)"

export PYTHONPATH="$PROJECT_ROOT"
python3 -m core.scripts.host_bridge_cli "$@"
