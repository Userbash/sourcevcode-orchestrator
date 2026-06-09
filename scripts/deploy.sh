#!/bin/bash
set -euo pipefail

python3 -m core.scripts.pre_deploy_security_check

echo "[deploy] Running bridge diagnostics..."
bash core/scripts/bridge/exec.sh --init

echo "[deploy] Building images..."
bash scripts/build_abstracted.sh

echo "[deploy] Starting containers..."
bash scripts/start_manual.sh

echo "[deploy] Done."
