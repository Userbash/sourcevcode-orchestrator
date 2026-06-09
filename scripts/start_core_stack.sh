#!/bin/bash
set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BRIDGE_CMD="$PROJECT_ROOT/core/scripts/bridge/exec.sh"
COMPOSE_FILE="$PROJECT_ROOT/docker-compose.ai.yml"

if [ ! -x "$BRIDGE_CMD" ]; then
  echo "[ERROR] BridgeOS exec script not found or not executable: $BRIDGE_CMD"
  exit 1
fi

if [ ! -f "$COMPOSE_FILE" ]; then
  echo "[ERROR] Compose file not found: $COMPOSE_FILE"
  exit 1
fi

echo "Checking Ollama status..."
if ! curl -s http://localhost:11434/api/tags > /dev/null; then
  echo "Ollama is offline. Attempting to start Ollama container..."
  "$BRIDGE_CMD" podman run -d --name ollama -v ollama:/root/.ollama -p 11434:11434 docker.io/ollama/ollama || echo "Ollama container already exists or failed to start."
  "$BRIDGE_CMD" podman start ollama || true
  
  # Wait for Ollama to be ready
  for i in {1..10}; do
    if curl -s http://localhost:11434/api/tags > /dev/null; then
      echo "Ollama is ready."
      break
    fi
    echo "Waiting for Ollama... ($i/10)"
    sleep 2
  done
fi

echo "Starting AI Bridge stack from $COMPOSE_FILE..."

"$BRIDGE_CMD" podman compose -f "$COMPOSE_FILE" up -d --build

echo "AI Bridge stack is starting."
echo "Orchestrator: http://localhost:${ORCHESTRATOR_PORT:-8000}"
echo "Ollama: http://localhost:${AI_BRIDGE_LOCAL_LLM_PORT:-11434}"
