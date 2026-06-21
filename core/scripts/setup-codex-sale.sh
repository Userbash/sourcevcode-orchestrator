#!/usr/bin/env bash
# ==============================================================================
# ORCHESTRATOR_METADATA_START
# AGENT_TYPE: "sub-agent-bridge"
# TARGET_ENVIRONMENT: "bazzite-linux-flatpak"
# COMPATIBLE_APP: "com.visualstudio.code"
# SUPPORTED_MODES: ["openai-emulation", "direct-passthrough", "multi-agent-mesh"]
# AVAILABLE_MODELS: ["gpt-5.4-nano", "gpt-5.4-mini", "gpt-5.4", "gpt-5.5", "gpt-image-2", "gpt-4o-transcribe"]
# ORCHESTRATOR_METADATA_END
# ==============================================================================

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"
FLATPAK_VSCODE_DIR="${HOME}/.var/app/com.visualstudio.code"
CODEX_DIR="${FLATPAK_VSCODE_DIR}/config/.codex"
REPO_BRIDGE_ENV_PATH="${PROJECT_ROOT}/.env.bridge"
BRIDGE_ENV_PATH="${CODEX_DIR}/.env.bridge"
ENV_PATH="${CODEX_DIR}/codex-sale.env"
AUTH_PATH="${CODEX_DIR}/auth.json"
CONFIG_PATH="${CODEX_DIR}/config.toml"
BACKUP_DIR="${CODEX_DIR}/backups/codex-sale-$(date +%Y%m%d-%H%M%S)-$$"
PING_ONLY="false"

if [[ "${1:-}" == "--ping-only" ]]; then
  PING_ONLY="true"
fi

load_env_file() {
  local path="$1"
  [[ -f "$path" ]] || return 0
  set -a
  # shellcheck disable=SC1090
  source "$path"
  set +a
}

normalize_url() {
  local value="$1"
  value="${value%/}"
  printf '%s' "$value"
}

join_url() {
  local base suffix
  base="$(normalize_url "$1")"
  suffix="$2"
  printf '%s/%s' "$base" "${suffix#/}"
}

write_file() {
  local path="$1"
  local mode="$2"
  mkdir -p "$(dirname "$path")"
  sed 's/^        //' > "$path"
  chmod "$mode" "$path"
}

write_env_file() {
  local path="$1"
  local mode="${2:-600}"
  write_file "$path" "$mode" <<EOF
export CODEX_LB_API_KEY="$API_KEY"
export OPENAI_API_KEY="$API_KEY"
export OPENAI_BASE_URL="$OPENAI_BASE_URL"
export ORCHESTRATOR_SUB_AGENT_ACTIVE="true"
EOF
}

ping_codex_sale() {
  python3 - <<'PYINNER'
import json
import os
import sys
import urllib.error
import urllib.request

api_key = os.environ.get('API_KEY', '').strip()
base_url = os.environ.get('OPENAI_BASE_URL', '').strip().rstrip('/')
model = os.environ.get('DEFAULT_MODEL', 'gpt-5.4-mini').strip()
headers = {'Authorization': f'Bearer {api_key}', 'Content-Type': 'application/json'}
out = {}

for label, url, payload in [
    ('models', f'{base_url}/models', None),
    ('chat_completions', f'{base_url}/chat/completions', {'model': model, 'messages': [{'role': 'user', 'content': 'reply with ok'}], 'max_tokens': 8}),
    ('responses', f'{base_url}/responses', {'model': model, 'input': 'reply with ok', 'max_output_tokens': 8}),
]:
    try:
        if payload is None:
            req = urllib.request.Request(url, headers=headers)
        else:
            req = urllib.request.Request(url, data=json.dumps(payload).encode('utf-8'), headers=headers, method='POST')
        with urllib.request.urlopen(req, timeout=30) as resp:
            body = resp.read().decode('utf-8', 'ignore')[:240]
            out[label] = {'ok': True, 'status': getattr(resp, 'status', 200), 'body': body}
    except urllib.error.HTTPError as exc:
        body = exc.read().decode('utf-8', 'ignore')[:240]
        out[label] = {'ok': False, 'status': exc.code, 'body': body}
    except Exception as exc:
        out[label] = {'ok': False, 'status': None, 'body': str(exc)}

print(json.dumps(out, ensure_ascii=False, indent=2))
if not all(item.get('ok') for item in out.values()):
    sys.exit(1)
PYINNER
}

patch_vscode_codex_binaries() {
  local patched_count=0
  local codex_path=""
  local backup_path=""
  local root=""

  while IFS= read -r codex_path; do
    [[ -n "$codex_path" ]] || continue
    backup_path="${codex_path}.codex-sale-real"

    if [[ -f "$backup_path" ]]; then
      write_file "$codex_path" 700 <<EOF
#!/usr/bin/env bash
set -euo pipefail
source "$ENV_PATH"
exec "$backup_path" "\$@"
EOF
      patched_count=$((patched_count + 1))
      echo "[БРИДЖ] Обновлен wrapper расширения во Flatpak: $codex_path"
      continue
    fi

    mv "$codex_path" "$backup_path"
    write_file "$codex_path" 700 <<EOF
#!/usr/bin/env bash
set -euo pipefail
source "$ENV_PATH"
exec "$backup_path" "\$@"
EOF
    patched_count=$((patched_count + 1))
    echo "[БРИДЖ] Пропатчен бинарник расширения во Flatpak: $codex_path"
  done < <(
    for root in "${FLATPAK_VSCODE_DIR}/data/vscode/extensions"; do
      [[ -d "$root" ]] || continue
      find "$root" -type f -path '*/openai.chatgpt-*/bin/*/codex' 2>/dev/null
    done | sort -u
  )

  if [[ "$patched_count" -eq 0 ]]; then
    echo "КРИТИЧЕСКАЯ ОШИБКА: Расширение OpenAI / Codex не найдено внутри Flatpak VS Code."
    echo "Решение: откройте VS Code, установите расширение 'Codex' или официальное OpenAI, затем запустите этот скрипт снова."
    exit 1
  fi
}

stop_codex_processes() {
  local codex_pids
  codex_pids="$(ps -u "$(id -u)" -o pid=,args= | awk '/openai[.]chatgpt-.*\/bin\/.*\/codex app-server/ { print $1 }')"

  if [[ -z "$codex_pids" ]]; then
    echo "[СТАТУС] Демон Codex в данный момент не запущен."
    return 0
  fi

  echo "Остановка процессов Codex: $codex_pids"
  kill $codex_pids 2>/dev/null || true
  sleep 1
}

restart_flatpak_vscode() {
  stop_codex_processes
  echo "[ПЕРЕЗАПУСК] Завершаем работу Flatpak VS Code для применения моста оркестратора..."
  flatpak kill com.visualstudio.code 2>/dev/null || true
}

mkdir -p "$CODEX_DIR" "$BACKUP_DIR"
load_env_file "$BRIDGE_ENV_PATH"
load_env_file "$REPO_BRIDGE_ENV_PATH"

API_KEY="${OPENAI_API_KEY:-${CODEX_SALE_API_KEY:-}}"
BASE_URL="${CODEX_SALE_BASE_URL:-${OPENAI_BASE_URL:-https://codex.sale}}"
BASE_URL="$(normalize_url "$BASE_URL")"
if [[ "$BASE_URL" == */v1 ]]; then
  CODEX_ROOT_URL="${BASE_URL%/v1}"
  OPENAI_BASE_URL="$BASE_URL"
else
  CODEX_ROOT_URL="$BASE_URL"
  OPENAI_BASE_URL="$(join_url "$BASE_URL" 'v1')"
fi
DEFAULT_MODEL="${CODEX_OPENAI_MODEL:-${DEFAULT_MODEL:-gpt-5.4-mini}}"
MODEL_REASONING_EFFORT="${MODEL_REASONING_EFFORT:-high}"
export API_KEY OPENAI_BASE_URL DEFAULT_MODEL MODEL_REASONING_EFFORT CODEX_ROOT_URL

if [[ -z "$API_KEY" ]]; then
  echo "Ошибка: OPENAI_API_KEY или CODEX_SALE_API_KEY не задан в ${REPO_BRIDGE_ENV_PATH} или ${BRIDGE_ENV_PATH}."
  exit 1
fi

if [[ "$PING_ONLY" == "true" ]]; then
  ping_codex_sale
  exit 0
fi

if [[ ! -d "$FLATPAK_VSCODE_DIR" ]]; then
  echo "Ошибка: Окружение Flatpak VS Code не найдено по пути: $FLATPAK_VSCODE_DIR"
  echo "Убедитесь, что VS Code установлен через Flatpak (Flathub)."
  exit 1
fi

[[ -f "$AUTH_PATH" ]] && cp "$AUTH_PATH" "$BACKUP_DIR/auth.json"
[[ -f "$CONFIG_PATH" ]] && cp "$CONFIG_PATH" "$BACKUP_DIR/config.toml"
[[ -f "$ENV_PATH" ]] && cp "$ENV_PATH" "$BACKUP_DIR/codex-sale.env"

umask 077
write_env_file "$ENV_PATH" 600

cat > "$AUTH_PATH" <<EOF
{
  "auth_mode": "apikey",
  "OPENAI_API_KEY": "$API_KEY"
}
EOF

cat > "$CONFIG_PATH" <<EOF
model = "$DEFAULT_MODEL"
model_reasoning_effort = "$MODEL_REASONING_EFFORT"
model_provider = "codex-sale"

[model_providers.codex-sale]
name = "Codex Sale (Orchestrator Mesh)"
base_url = "${CODEX_ROOT_URL%/}/backend-api/codex"
wire_api = "responses"
env_key = "CODEX_LB_API_KEY"
supports_websockets = true
requires_openai_auth = true

[orchestrator_routing]
chat_completions_endpoint = "${CODEX_ROOT_URL%/}/v1/chat/completions"
models_endpoint = "${CODEX_ROOT_URL%/}/v1/models"
responses_endpoint = "${CODEX_ROOT_URL%/}/v1/responses"
EOF

chmod 600 "$AUTH_PATH" "$CONFIG_PATH" "$ENV_PATH"
ping_codex_sale
patch_vscode_codex_binaries
restart_flatpak_vscode

echo "=========================================================================="
echo " УСПЕШНО: Мост ИИ-Оркестратора для Flatpak VS Code развернут!"
echo "=========================================================================="
echo " Источник секретов:                 $REPO_BRIDGE_ENV_PATH"
echo " Локальный файл управления:         $BRIDGE_ENV_PATH"
echo " Конфиг моста:                      $CONFIG_PATH"
echo "=========================================================================="
echo "OpenAI-compatible трафик для Codex направлен через $OPENAI_BASE_URL"
echo "Запустите ваш Visual Studio Code Flatpak заново."
