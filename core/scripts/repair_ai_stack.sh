#!/bin/bash
# Script to automatically verify and repair AI provider stack

# Colors
GREEN='\033[0;32m'
RED='\033[0;31m'
YELLOW='\033[1;33m'
NC='\033[0m'

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$PROJECT_ROOT"

echo "=============================================="
echo "   AI Provider Stack Maintainer & Repair"
echo "=============================================="

# 1. Antigravity (agy)
echo -n "[Check] Antigravity runtime... "
if flatpak-spawn --host bash -lc 'command -v agy >/dev/null 2>&1 || command -v antigravity >/dev/null 2>&1 || command -v gemini >/dev/null 2>&1'; then
    echo -e "${GREEN}INSTALLED${NC}"
    AGY_REPORT=$(flatpak-spawn --host python3 "$PROJECT_ROOT/core/scripts/verify_antigravity_keys.py" 2>/dev/null || true)
    echo "$AGY_REPORT"
    if echo "$AGY_REPORT" | grep -q '"ready": true'; then
        echo -e "${GREEN}[OK] Antigravity is ready${NC}"
    elif echo "$AGY_REPORT" | grep -q 'legacy_gemini_cli\|unsupported_client'; then
        echo -e "${YELLOW}[WARN] Legacy Gemini CLI detected; login loop suppressed. Install supported Antigravity runtime or fix API mode.${NC}"
    else
        echo -e "${YELLOW}[WARN] Antigravity is degraded. Review verify_antigravity_keys output before retrying login.${NC}"
    fi
else
    echo -e "${RED}NOT FOUND${NC}"
    echo "Install a supported Antigravity-compatible CLI on the host, or keep the provider disabled."
fi

# 2. Sourcecraft (src)
echo -n "[Check] Sourcecraft CLI (src)... "
SRC_BIN="./.tooling/sourcecraft/bin/src"
if [[ -x "$SRC_BIN" ]]; then
    echo -e "${GREEN}FOUND${NC}"
else
    echo -e "${RED}MISSING${NC}"
    echo "Sourcecraft binary not found in $SRC_BIN"
fi

# 3. API Keys (Mistral & Codex)
echo "[Check] API Keys..."
if [[ -f ".env.bridge" ]]; then
    # Parse .env.bridge manually since it might have export or just key=val
    M_KEY=$(grep "MISTRAL_API_KEY" .env.bridge | cut -d'=' -f2)
    O_KEY=$(grep "OPENAI_API_KEY" .env.bridge | cut -d'=' -f2)
    
    if [[ -n "$M_KEY" ]]; then
        echo -e "  - Mistral: ${GREEN}OK${NC}"
    else
        echo -e "  - Mistral: ${RED}MISSING KEY${NC}"
    fi
    if [[ -n "$O_KEY" ]]; then
        echo -e "  - Codex (OpenAI): ${GREEN}OK${NC}"
    else
        echo -e "  - Codex (OpenAI): ${RED}MISSING KEY${NC}"
    fi
else
    echo -e "${RED}.env.bridge not found${NC}"
fi

# 4. Local LLM (Ollama)
echo -n "[Check] Local LLM (Ollama)... "
if curl -s http://127.0.0.1:11434/api/tags &>/dev/null; then
    echo -e "${GREEN}RUNNING${NC}"
else
    echo -e "${YELLOW}NOT RESPONDING${NC}"
    echo "Note: Ensure Ollama is running on 127.0.0.1:11434 if local models are used."
fi

check_control_ws() {
    local base_url="$1"
    python3 - "$PROJECT_ROOT" "$base_url" <<'PY' >/dev/null 2>&1
import sys
from pathlib import Path

project_root = Path(sys.argv[1])
base_url = sys.argv[2]
sys.path.insert(0, str(project_root))

from core.core.control_ws_client import run_control_ws_action_sync

result = run_control_ws_action_sync(base_url, "stats.get", timeout_sec=5.0)
result.require_success()
raise SystemExit(0)
PY
}

# 5. Core Connection
echo -n "[Check] Orchestrator Core... "
CORE_URL=""
if check_control_ws http://localhost:8000; then
    CORE_URL="http://localhost:8000"
elif check_control_ws http://localhost:8001; then
    CORE_URL="http://localhost:8001"
fi

if [[ -n "$CORE_URL" ]]; then
    echo -e "${GREEN}CONNECTED ($CORE_URL via /control/ws)${NC}"
else
    echo -e "${YELLOW}OFFLINE${NC}"
    echo "Starting Orchestrator..."
    ./core/scripts/start_core_stack.sh
fi

echo "=============================================="
echo "   Repair Complete"
echo "=============================================="
