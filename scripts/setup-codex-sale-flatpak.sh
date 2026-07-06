#!/usr/bin/env bash
set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
FLATPAK_VSCODE_DIR="${HOME}/.var/app/com.visualstudio.code"
CODEX_DIR="${FLATPAK_VSCODE_DIR}/config/.codex"
CONFIG_PATH="${CODEX_DIR}/config.toml"
AUTH_PATH="${CODEX_DIR}/auth.json"
ENV_PATH="${CODEX_DIR}/codex-sale.env"
MODEL="${CODEX_OPENAI_MODEL:-gpt-5.4}"
REASONING="${MODEL_REASONING_EFFORT:-high}"
BASE_URL="${CODEX_SALE_BASE_URL:-https://codex.sale}"
RESTART_VSCODE=1

usage() {
  cat <<USAGE
Usage: $(basename "$0") [--api-key KEY] [--model MODEL] [--reasoning LEVEL] [--no-restart]
USAGE
}

API_KEY="${CODEX_LB_API_KEY:-${OPENAI_API_KEY:-${CODEX_SALE_API_KEY:-}}}"
while [ "$#" -gt 0 ]; do
  case "$1" in
    --api-key)
      API_KEY="$2"
      shift 2
      ;;
    --model)
      MODEL="$2"
      shift 2
      ;;
    --reasoning)
      REASONING="$2"
      shift 2
      ;;
    --no-restart)
      RESTART_VSCODE=0
      shift
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "Unknown option: $1" >&2
      usage >&2
      exit 1
      ;;
  esac
done

if [ -z "$API_KEY" ]; then
  echo "Missing API key. Pass --api-key or set CODEX_LB_API_KEY / OPENAI_API_KEY." >&2
  exit 1
fi

CODEX_ROOT_URL="${BASE_URL%/}"
OPENAI_BASE_URL="${CODEX_ROOT_URL}/v1"
mkdir -p "$CODEX_DIR"
umask 077

cat > "$CONFIG_PATH" <<EOF
model = "$MODEL"
model_reasoning_effort = "$REASONING"
model_provider = "codex-sale"

[model_providers.codex-sale]
name = "OpenAI"
base_url = "${CODEX_ROOT_URL}/backend-api/codex"
wire_api = "responses"
env_key = "CODEX_LB_API_KEY"
supports_websockets = true
requires_openai_auth = true
EOF

cat > "$AUTH_PATH" <<EOF
{
  "auth_mode": "apikey",
  "OPENAI_API_KEY": "$API_KEY"
}
EOF

cat > "$ENV_PATH" <<EOF
export CODEX_LB_API_KEY="$API_KEY"
export OPENAI_API_KEY="$API_KEY"
export CODEX_SALE_API_KEY="$API_KEY"
export CODEX_OPENAI_MODEL="$MODEL"
export MODEL_REASONING_EFFORT="$REASONING"
export CODEX_SALE_BASE_URL="$CODEX_ROOT_URL"
export OPENAI_BASE_URL="$OPENAI_BASE_URL"
export CODEX_ROOT_URL="$CODEX_ROOT_URL"
EOF

chmod 600 "$CONFIG_PATH" "$AUTH_PATH" "$ENV_PATH"

export CODEX_LB_API_KEY="$API_KEY"
export OPENAI_API_KEY="$API_KEY"
export CODEX_SALE_API_KEY="$API_KEY"
export CODEX_OPENAI_MODEL="$MODEL"
export MODEL_REASONING_EFFORT="$REASONING"
export CODEX_SALE_BASE_URL="$CODEX_ROOT_URL"
export OPENAI_BASE_URL="$OPENAI_BASE_URL"
export CODEX_ROOT_URL="$CODEX_ROOT_URL"
export AI_BRIDGE_CODEX_CONFIG_DIR="$CODEX_DIR"

python3 "$PROJECT_ROOT/core/scripts/collect_bazzite_openai_endpoint.py"
python3 "$PROJECT_ROOT/core/scripts/verify_openai_bridge.py"

python3 - <<'PYINNER'
import json
import os
import sys
import urllib.request

base = os.environ['OPENAI_BASE_URL'].rstrip('/')
key = os.environ['OPENAI_API_KEY'].strip()
req = urllib.request.Request(
    f"{base}/models",
    headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
)
with urllib.request.urlopen(req, timeout=30) as resp:
    payload = json.loads(resp.read().decode('utf-8'))
models = []
for item in payload.get('data', []) if isinstance(payload, dict) else []:
    model_id = str((item or {}).get('id') or '').strip()
    if model_id:
        models.append(model_id)
print(json.dumps({
    "provider_id": "codexsale",
    "provider_name": "Codex Sale",
    "base_endpoint": f"{base}",
    "models_endpoint": f"{base}/models",
    "chat_endpoint": f"{base}/chat/completions",
    "responses_endpoint": f"{base}/responses",
    "messages_endpoint": f"{base}/messages",
    "messages_count_tokens_endpoint": f"{base}/messages/count_tokens",
    "codex_endpoint": f"{base[:-3]}/backend-api/codex" if base.endswith('/v1') else f"{base}/backend-api/codex",
    "model_count": len(models),
    "models": models,
}, ensure_ascii=False, indent=2))
PYINNER

if [ "$RESTART_VSCODE" -eq 1 ]; then
  if command -v flatpak >/dev/null 2>&1; then
    flatpak kill com.visualstudio.code 2>/dev/null || true
  fi
fi

echo "Configured $CODEX_DIR for codex.sale with model=$MODEL reasoning=$REASONING"
