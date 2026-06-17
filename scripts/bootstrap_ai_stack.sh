#!/usr/bin/env bash
set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ORCHESTRATOR_IMAGE="${ORCHESTRATOR_IMAGE:-localhost/hebrew-orchestrator:latest}"
ORCHESTRATOR_CONTAINER="${ORCHESTRATOR_CONTAINER:-hebrew_ai_orchestrator}"
POSTGRES_CONTAINER="${POSTGRES_CONTAINER:-ai_bridge_db}"
RABBITMQ_CONTAINER="${RABBITMQ_CONTAINER:-ai_bridge_rabbitmq}"
OLLAMA_CONTAINER="${OLLAMA_CONTAINER:-ollama}"
PG_VOLUME_NAME="${AI_BRIDGE_PG_DATA_VOLUME_NAME:-hebrew_pg_data}"
MEMORY_VOLUME_NAME="${AI_BRIDGE_MEMORY_VOLUME_NAME:-hebrew_core_memory}"
OLLAMA_VOLUME_NAME="${AI_BRIDGE_OLLAMA_VOLUME_NAME:-ollama}"
LOCAL_MODEL="${AI_BRIDGE_LOCAL_LLM_MODEL:-qwen2.5:0.5b}"
ORCHESTRATOR_PORT="${ORCHESTRATOR_PORT:-8000}"
OLLAMA_PORT="${AI_BRIDGE_LOCAL_LLM_PORT:-11434}"
RUN_LOCAL_LLM=1
RUN_AGY_LOGIN=0
BUILD_IMAGE=1

host_run() {
  if command -v flatpak-spawn >/dev/null 2>&1; then
    flatpak-spawn --host "$@"
  else
    "$@"
  fi
}

usage() {
  cat <<USAGE
Usage: $(basename "$0") [options]

Options:
  --model NAME           Ollama model to pull. Default: ${LOCAL_MODEL}
  --skip-local-llm       Do not start/pull local Ollama model.
  --agy-login            Run Antigravity login helper if agy is installed.
  --no-build             Reuse existing orchestrator image.
  -h, --help             Show this help.
USAGE
}

while [ "$#" -gt 0 ]; do
  case "$1" in
    --model)
      LOCAL_MODEL="$2"
      shift 2
      ;;
    --skip-local-llm)
      RUN_LOCAL_LLM=0
      shift
      ;;
    --agy-login)
      RUN_AGY_LOGIN=1
      shift
      ;;
    --no-build)
      BUILD_IMAGE=0
      shift
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "[ERROR] Unknown option: $1" >&2
      usage >&2
      exit 1
      ;;
  esac
done

log() {
  printf '[bootstrap] %s\n' "$*"
}

warn() {
  printf '[bootstrap][warn] %s\n' "$*" >&2
}

ensure_file() {
  local target="$1"
  local template="$2"
  if [ ! -f "$target" ]; then
    cp "$template" "$target"
    log "Created $(basename "$target") from $(basename "$template")"
  fi
}

ensure_env_files() {
  ensure_file "$PROJECT_ROOT/.env" "$PROJECT_ROOT/.env.example"
  ensure_file "$PROJECT_ROOT/.env.bridge" "$PROJECT_ROOT/.env.bridge.example"
  ensure_file "$PROJECT_ROOT/.env.gemini.local" "$PROJECT_ROOT/.env.gemini.local.example"
}

load_env_files() {
  set -a
  . "$PROJECT_ROOT/.env"
  . "$PROJECT_ROOT/.env.bridge"
  . "$PROJECT_ROOT/.env.gemini.local"
  set +a

  LOCAL_MODEL="${AI_BRIDGE_LOCAL_LLM_MODEL:-$LOCAL_MODEL}"
  ORCHESTRATOR_PORT="${ORCHESTRATOR_PORT:-$ORCHESTRATOR_PORT}"
  OLLAMA_PORT="${AI_BRIDGE_LOCAL_LLM_PORT:-$OLLAMA_PORT}"
}

ensure_host_requirements() {
  if ! host_run podman --version >/dev/null 2>&1; then
    echo "[ERROR] podman is required on the host." >&2
    exit 1
  fi
  if ! host_run curl --version >/dev/null 2>&1; then
    echo "[ERROR] curl is required on the host." >&2
    exit 1
  fi
}


ensure_volume() {
  local volume_name="$1"
  host_run podman volume exists "$volume_name" >/dev/null 2>&1 || host_run podman volume create "$volume_name" >/dev/null
}

remove_container_if_exists() {
  local name="$1"
  if host_run podman container exists "$name" >/dev/null 2>&1; then
    host_run podman rm -f -v "$name" >/dev/null
  fi
}

wait_for_http() {
  local url="$1"
  local attempts="$2"
  local sleep_sec="$3"
  local i
  for i in $(seq 1 "$attempts"); do
    if host_run curl -fsS "$url" >/dev/null 2>&1; then
      return 0
    fi
    sleep "$sleep_sec"
  done
  return 1
}

wait_for_pg() {
  local i
  for i in $(seq 1 45); do
    if host_run podman exec "$POSTGRES_CONTAINER" pg_isready -U ai_bridge -d ai_bridge >/dev/null 2>&1; then
      return 0
    fi
    sleep 1
  done
  return 1
}

wait_for_rabbitmq() {
  local i
  for i in $(seq 1 45); do
    if host_run podman exec "$RABBITMQ_CONTAINER" rabbitmq-diagnostics -q ping >/dev/null 2>&1; then
      return 0
    fi
    sleep 1
  done
  return 1
}

start_postgres() {
  log "Starting Postgres container..."
  if host_run podman container exists "$POSTGRES_CONTAINER" >/dev/null 2>&1; then
    host_run podman start "$POSTGRES_CONTAINER" >/dev/null || true
  else
    host_run podman run -d \
      --name "$POSTGRES_CONTAINER" \
      -p 5432:5432 \
      -e POSTGRES_USER=ai_bridge \
      -e POSTGRES_PASSWORD=ai_bridge_password \
      -e POSTGRES_DB=ai_bridge \
      -v "${PG_VOLUME_NAME}:/var/lib/postgresql/data" \
      docker.io/library/postgres:16-alpine \
      postgres -c shared_buffers=256MB -c max_connections=200 -c effective_cache_size=768MB -c work_mem=16MB >/dev/null
  fi
  wait_for_pg || {
    host_run podman logs --tail 120 "$POSTGRES_CONTAINER" >&2 || true
    echo "[ERROR] Postgres did not become ready." >&2
    exit 1
  }
}

start_rabbitmq() {
  log "Starting RabbitMQ container..."
  if host_run podman container exists "$RABBITMQ_CONTAINER" >/dev/null 2>&1; then
    host_run podman start "$RABBITMQ_CONTAINER" >/dev/null || true
  else
    host_run podman run -d \
      --name "$RABBITMQ_CONTAINER" \
      --user 0 \
      -p 5672:5672 \
      -p 15672:15672 \
      -e RABBITMQ_DEFAULT_USER=guest \
      -e RABBITMQ_DEFAULT_PASS=guest \
      docker.io/library/rabbitmq:3-management >/dev/null
  fi
  wait_for_rabbitmq || {
    host_run podman logs --tail 120 "$RABBITMQ_CONTAINER" >&2 || true
    echo "[ERROR] RabbitMQ did not become ready." >&2
    exit 1
  }
}

start_local_llm() {
  log "Ensuring local Ollama endpoint on 127.0.0.1:${OLLAMA_PORT}..."
  if host_run curl -fsS "http://127.0.0.1:${OLLAMA_PORT}/api/tags" >/dev/null 2>&1; then
    log "Existing Ollama endpoint is already reachable."
  else
    if ! host_run podman container exists "$OLLAMA_CONTAINER" >/dev/null 2>&1; then
      ensure_volume "$OLLAMA_VOLUME_NAME"
      host_run podman run -d \
        --name "$OLLAMA_CONTAINER" \
        -p "${OLLAMA_PORT}:11434" \
        -v "${OLLAMA_VOLUME_NAME}:/root/.ollama" \
        docker.io/ollama/ollama >/dev/null
    else
      host_run podman start "$OLLAMA_CONTAINER" >/dev/null || true
    fi
    wait_for_http "http://127.0.0.1:${OLLAMA_PORT}/api/tags" 45 1 || {
      host_run podman logs --tail 120 "$OLLAMA_CONTAINER" >&2 || true
      echo "[ERROR] Ollama endpoint is not reachable on port ${OLLAMA_PORT}." >&2
      exit 1
    }
  fi

  log "Pulling local model ${LOCAL_MODEL}..."
  host_run podman exec "$OLLAMA_CONTAINER" ollama pull "$LOCAL_MODEL"
}

build_orchestrator_image() {
  if [ "$BUILD_IMAGE" -eq 0 ]; then
    log "Skipping image build. Reusing ${ORCHESTRATOR_IMAGE}."
    return
  fi
  log "Building orchestrator image ${ORCHESTRATOR_IMAGE}..."
  host_run podman build -f "$PROJECT_ROOT/core/Dockerfile" -t "$ORCHESTRATOR_IMAGE" "$PROJECT_ROOT"
}

start_orchestrator() {
  log "Starting orchestrator container..."
  remove_container_if_exists "$ORCHESTRATOR_CONTAINER"
  host_run podman run -d \
    --name "$ORCHESTRATOR_CONTAINER" \
    -w /app \
    -p "${ORCHESTRATOR_PORT}:8000" \
    --env-file "$PROJECT_ROOT/.env" \
    --env-file "$PROJECT_ROOT/.env.gemini.local" \
    --env-file "$PROJECT_ROOT/.env.bridge" \
    -e PYTHONPATH=/app \
    -e PATH=/var/home/sanya/.npm-packages/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin:/app/.local/bin \
    -e NODE_PATH=/var/home/sanya/.npm-packages/lib/node_modules \
    -e TESTING=false \
    -e AI_BRIDGE_AUTOSTART_LOCAL_LLM=false \
    -e AI_BRIDGE_AUTOSTART_EASY_DIFFUSION=false \
    -e AI_BRIDGE_LOCAL_LLM_AUTO_PROVISION=false \
    -e AI_BRIDGE_LOCAL_LLM_ENDPOINT="http://host.containers.internal:${OLLAMA_PORT}" \
    -e AI_BRIDGE_LOCAL_LLM_PORT="${OLLAMA_PORT}" \
    -e AI_BRIDGE_LOCAL_LLM_MODEL="${LOCAL_MODEL}" \
    -e AI_BRIDGE_WORKSPACE_ROOT=/workspace \
    -e AI_BRIDGE_EASY_DIFFUSION_START_ENABLED=false \
    -e AI_BRIDGE_MEMORY_ENABLED=true \
    -e AI_BRIDGE_MEMORY_DATABASE_URL="postgresql+psycopg2://ai_bridge:ai_bridge_password@host.containers.internal:5432/ai_bridge" \
    -e AI_BRIDGE_RABBITMQ_URL="amqp://guest:guest@host.containers.internal:5672/" \
    -e AI_BRIDGE_MESSAGE_BUS_BACKEND=rabbitmq \
    -e AI_BRIDGE_MEMORY_STORE_DIR=/app/memory_store \
    -e AI_BRIDGE_LIVE_MODEL_PROBE=false \
    -e AI_BRIDGE_REQUIRE_EXTERNAL_SCANNERS=false \
    -e AI_BRIDGE_DISABLE_SOURCECRAFT=true \
    -e AI_BRIDGE_ENABLE_VOICE=false \
    -e AI_BRIDGE_AUTO_APPROVE=true \
    -e AI_BRIDGE_CONFIRMATION_POLICY=full_auto \
    -e OPENAI_SESSION_TOKEN_BUDGET=120000 \
    -v "${MEMORY_VOLUME_NAME}:/app/memory_store" \
    -v "/var/home/sanya/.npm-packages:/var/home/sanya/.npm-packages:ro,z" \
    -v "$PROJECT_ROOT/.env.bridge:/app/.env.bridge:ro,z" \
    -v "$PROJECT_ROOT/scripts:/app/scripts:ro,z" \
    -v "$PROJECT_ROOT:/workspace:z" \
    "$ORCHESTRATOR_IMAGE" >/dev/null

  wait_for_http "http://127.0.0.1:${ORCHESTRATOR_PORT}/health" 45 1 || {
    host_run podman logs --tail 120 "$ORCHESTRATOR_CONTAINER" >&2 || true
    echo "[ERROR] Orchestrator health endpoint did not become ready." >&2
    exit 1
  }
}

verify_mistral() {
  if grep -Eq '^MISTRAL_API_KEY=.+' "$PROJECT_ROOT/.env.bridge"; then
    log "Verifying Mistral connectivity..."
    host_run podman exec "$ORCHESTRATOR_CONTAINER" python -m core.scripts.verify_mistral_bridge || warn "Mistral verification failed."
  else
    warn "MISTRAL_API_KEY is not set in .env.bridge. Mistral provider will stay degraded."
  fi
}

verify_agy() {
  if ! host_run bash -lc 'command -v agy >/dev/null 2>&1' >/dev/null 2>&1; then
    warn "agy is not installed on the host. Antigravity provider will stay degraded until agy is installed and authorized."
    return
  fi

  if [ "$RUN_AGY_LOGIN" -eq 1 ]; then
    log "Running Antigravity login helper..."
    host_run python3 "$PROJECT_ROOT/core/scripts/antigravity_login.py" --login --timeout 300 || warn "Antigravity login helper did not confirm readiness."
  else
    log "Checking Antigravity authorization state..."
    host_run python3 "$PROJECT_ROOT/core/scripts/antigravity_login.py" --verify || warn "Antigravity is installed but not yet authorized. Re-run with --agy-login to complete OAuth."
  fi
}

print_summary() {
  log "AI stack is ready."
  printf '  Orchestrator: http://127.0.0.1:%s/health\n' "$ORCHESTRATOR_PORT"
  printf '  RabbitMQ UI:  http://127.0.0.1:15672 (guest / guest)\n'
  printf '  Postgres:     127.0.0.1:5432 (ai_bridge / ai_bridge_password / ai_bridge)\n'
  if [ "$RUN_LOCAL_LLM" -eq 1 ]; then
    printf '  Local LLM:    http://127.0.0.1:%s (model: %s)\n' "$OLLAMA_PORT" "$LOCAL_MODEL"
  fi
  printf '\nNext steps:\n'
  printf '  1. Add OPENAI_API_KEY and/or MISTRAL_API_KEY to .env.bridge if you need remote providers.\n'
  printf '  2. Install and authorize agy, then rerun %s --agy-login if you need Antigravity CLI.\n' "$0"
  printf '  3. Check full provider state: curl -fsS http://127.0.0.1:%s/health/full\n' "$ORCHESTRATOR_PORT"
}

main() {
  cd "$PROJECT_ROOT"
  ensure_host_requirements
  ensure_env_files
  load_env_files
  ensure_volume "$PG_VOLUME_NAME"
  ensure_volume "$MEMORY_VOLUME_NAME"

  if [ "$RUN_LOCAL_LLM" -eq 1 ]; then
    start_local_llm
  fi

  start_postgres
  start_rabbitmq
  build_orchestrator_image
  start_orchestrator
  verify_mistral
  verify_agy
  print_summary
}

main "$@"
