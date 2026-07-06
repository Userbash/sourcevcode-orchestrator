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
ORCHESTRATOR_CONTAINER_PORT="${ORCHESTRATOR_CONTAINER_PORT:-8000}"
OLLAMA_PORT="${AI_BRIDGE_LOCAL_LLM_PORT:-11434}"
AI_KERNEL_PORT="${AI_KERNEL_PORT:-8012}"
ORCHESTRATOR_HEALTH_PATH="${ORCHESTRATOR_HEALTH_PATH:-/health}"
ORCHESTRATOR_READY_ATTEMPTS="${ORCHESTRATOR_READY_ATTEMPTS:-120}"
ORCHESTRATOR_READY_SLEEP_SEC="${ORCHESTRATOR_READY_SLEEP_SEC:-2}"
RUN_LOCAL_LLM=1
RUN_AI_KERNEL=1
RUN_AGY_LOGIN=0
BUILD_IMAGE=1
AI_BRIDGE_POSTGRES_PASSWORD="${AI_BRIDGE_POSTGRES_PASSWORD:-change_me_local_db_password}"
AI_BRIDGE_RABBITMQ_PASSWORD="${AI_BRIDGE_RABBITMQ_PASSWORD:-change_me_local_rabbitmq_password}"

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
  --skip-ai-kernel       Do not start/verify local AI Kernel on port ${AI_KERNEL_PORT}.
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
    --skip-ai-kernel)
      RUN_AI_KERNEL=0
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
  local file
  for file in "$PROJECT_ROOT/.env" "$PROJECT_ROOT/.env.bridge" "$PROJECT_ROOT/.env.gemini.local"; do
    set +e
    set -a
    . "$file"
    local rc=$?
    set +a
    set -e
    if [ "$rc" -ne 0 ]; then
      echo "[ERROR] Failed to load env file: $file" >&2
      echo "[ERROR] Quote values that contain spaces, for example: VAR='command with args'" >&2
      exit "$rc"
    fi
  done

  LOCAL_MODEL="${AI_BRIDGE_LOCAL_LLM_MODEL:-$LOCAL_MODEL}"
  ORCHESTRATOR_PORT="${ORCHESTRATOR_PORT:-$ORCHESTRATOR_PORT}"
  OLLAMA_PORT="${AI_BRIDGE_LOCAL_LLM_PORT:-$OLLAMA_PORT}"
  AI_KERNEL_PORT="${AI_KERNEL_PORT:-$AI_KERNEL_PORT}"
  ORCHESTRATOR_HEALTH_PATH="${ORCHESTRATOR_HEALTH_PATH:-$ORCHESTRATOR_HEALTH_PATH}"
  ORCHESTRATOR_READY_ATTEMPTS="${ORCHESTRATOR_READY_ATTEMPTS:-$ORCHESTRATOR_READY_ATTEMPTS}"
  ORCHESTRATOR_READY_SLEEP_SEC="${ORCHESTRATOR_READY_SLEEP_SEC:-$ORCHESTRATOR_READY_SLEEP_SEC}"
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

detect_local_llm_gpu_backend() {
  if [ "${AI_BRIDGE_LOCAL_LLM_GPU_BACKEND:-auto}" = "cpu" ]; then
    echo cpu
    return 0
  fi
  if [ "${AI_BRIDGE_LOCAL_LLM_GPU_BACKEND:-auto}" = "nvidia" ]; then
    echo nvidia
    return 0
  fi
  if host_run bash -lc 'command -v nvidia-smi >/dev/null 2>&1'; then
    echo nvidia
    return 0
  fi
  echo cpu
}

ollama_container_has_nvidia_gpu() {
  local inspect
  inspect="$(host_run podman inspect "$OLLAMA_CONTAINER" --format '{{json .HostConfig.Devices}} {{json .HostConfig.SecurityOpt}} {{json .HostConfig.GroupAdd}}' 2>/dev/null || true)"
  printf '%s' "$inspect" | grep -q 'nvidia.com/gpu=all' || return 1
  printf '%s' "$inspect" | grep -q 'label=disable' || return 1
  printf '%s' "$inspect" | grep -q 'keep-groups' || return 1
}

recreate_ollama_container_with_gpu() {
  local backend="$1"
  remove_container_if_exists "$OLLAMA_CONTAINER"
  ensure_volume "$OLLAMA_VOLUME_NAME"
  if [ "$backend" = "nvidia" ]; then
    host_run podman run -d       --name "$OLLAMA_CONTAINER"       -p "${OLLAMA_PORT}:11434"       --security-opt=label=disable       --group-add keep-groups       --device nvidia.com/gpu=all       -e NVIDIA_VISIBLE_DEVICES=all       -e NVIDIA_DRIVER_CAPABILITIES=compute,utility       -v "${OLLAMA_VOLUME_NAME}:/root/.ollama"       docker.io/ollama/ollama >/dev/null
    return 0
  fi
  host_run podman run -d     --name "$OLLAMA_CONTAINER"     -p "${OLLAMA_PORT}:11434"     -v "${OLLAMA_VOLUME_NAME}:/root/.ollama"     docker.io/ollama/ollama >/dev/null
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
    if host_run curl --max-time 5 -fsS "$url" >/dev/null 2>&1; then
      return 0
    fi
    sleep "$sleep_sec"
  done
  return 1
}

wait_for_ai_kernel() {
  wait_for_http "http://127.0.0.1:${AI_KERNEL_PORT}/v1/models" 90 2
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
      -e POSTGRES_PASSWORD="$AI_BRIDGE_POSTGRES_PASSWORD" \
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
      -e RABBITMQ_DEFAULT_PASS="$AI_BRIDGE_RABBITMQ_PASSWORD" \
      docker.io/library/rabbitmq:3-management >/dev/null
  fi
  wait_for_rabbitmq || {
    host_run podman logs --tail 120 "$RABBITMQ_CONTAINER" >&2 || true
    echo "[ERROR] RabbitMQ did not become ready." >&2
    exit 1
  }
}

start_local_llm() {
  local backend
  backend="$(detect_local_llm_gpu_backend)"
  log "Ensuring local Ollama endpoint on 127.0.0.1:${OLLAMA_PORT}..."
  if host_run podman container exists "$OLLAMA_CONTAINER" >/dev/null 2>&1; then
    if [ "$backend" = "nvidia" ] && ! ollama_container_has_nvidia_gpu; then
      warn "Existing Ollama container has no real NVIDIA passthrough; recreating it with GPU access."
      recreate_ollama_container_with_gpu "$backend"
    else
      host_run podman start "$OLLAMA_CONTAINER" >/dev/null || true
    fi
  else
    recreate_ollama_container_with_gpu "$backend"
  fi

  wait_for_http "http://127.0.0.1:${OLLAMA_PORT}/api/tags" 45 1 || {
    host_run podman logs --tail 120 "$OLLAMA_CONTAINER" >&2 || true
    echo "[ERROR] Ollama endpoint is not reachable on port ${OLLAMA_PORT}." >&2
    exit 1
  }

  if host_run sh -lc "curl --max-time 10 -fsS http://127.0.0.1:${OLLAMA_PORT}/api/tags | tr -d '[:space:]' | grep -F '\"name\":\"${LOCAL_MODEL}\"' >/dev/null"; then
    log "Local model ${LOCAL_MODEL} is already present. Skipping pull."
  else
    log "Pulling local model ${LOCAL_MODEL}..."
    host_run podman exec "$OLLAMA_CONTAINER" ollama pull "$LOCAL_MODEL"
  fi
}

build_orchestrator_image() {
  if [ "$BUILD_IMAGE" -eq 0 ]; then
    log "Skipping image build. Reusing ${ORCHESTRATOR_IMAGE}."
    return
  fi
  log "Building orchestrator image ${ORCHESTRATOR_IMAGE}..."
  host_run podman build -f "$PROJECT_ROOT/core/Dockerfile" -t "$ORCHESTRATOR_IMAGE" "$PROJECT_ROOT"
}

start_ai_kernel() {
  case "${AI_KERNEL_ENABLED:-true}" in
    1|true|yes|on) ;;
    *)
      log "AI Kernel disabled by env. Skipping host-side startup."
      return 0
      ;;
  esac

  local install_script="$PROJECT_ROOT/scripts/ai-kernel/install_hauhaucs_qwen36.sh"
  local serve_script="$PROJECT_ROOT/scripts/ai-kernel/serve_hauhaucs_qwen36_q4km.sh"
  local runtime_python="${AI_KERNEL_RUNTIME_PYTHON:-${XDG_CACHE_HOME:-$HOME/.cache}/ai-kernel/venvs/llama-cpp/bin/python}"
  local log_path="${AI_KERNEL_LOG_PATH:-/tmp/ai-kernel-server.log}"
  local pid_path="${AI_KERNEL_PID_PATH:-/tmp/ai-kernel-server.pid}"

  if wait_for_ai_kernel; then
    log "AI Kernel is already reachable on 127.0.0.1:${AI_KERNEL_PORT}."
    return 0
  fi

  if [ ! -x "$runtime_python" ] || ! host_run "$runtime_python" -c "import llama_cpp, llama_cpp.server" >/dev/null 2>&1; then
    log "Installing AI Kernel runtime dependencies..."
    host_run bash "$install_script"
  fi

  if [ -f "$pid_path" ]; then
    local existing_pid
    existing_pid="$(cat "$pid_path" 2>/dev/null || true)"
    if [ -n "$existing_pid" ] && host_run sh -lc "kill -0 $existing_pid" >/dev/null 2>&1; then
      log "AI Kernel process already running with pid $existing_pid."
    else
      rm -f "$pid_path"
    fi
  fi

  if ! wait_for_ai_kernel; then
    log "Starting AI Kernel on 127.0.0.1:${AI_KERNEL_PORT} via systemd user service..."
    host_run bash "$PROJECT_ROOT/scripts/ai-kernel/install_user_service.sh"
    host_run systemctl --user start "${AI_KERNEL_SERVICE_NAME:-ai-kernel.service}"
  fi

  wait_for_ai_kernel || {
    if [ -f "$log_path" ]; then
      tail -n 120 "$log_path" >&2 || true
    fi
    echo "[ERROR] AI Kernel did not become ready on port ${AI_KERNEL_PORT}." >&2
    exit 1
  }
}

start_orchestrator() {
  log "Starting orchestrator container..."
  remove_container_if_exists "$ORCHESTRATOR_CONTAINER"
  host_run podman run -d \
    --name "$ORCHESTRATOR_CONTAINER" \
    -w /app \
    -p "${ORCHESTRATOR_PORT}:${ORCHESTRATOR_CONTAINER_PORT}" \
    --env-file "$PROJECT_ROOT/.env" \
    --env-file "$PROJECT_ROOT/.env.gemini.local" \
    --env-file "$PROJECT_ROOT/.env.bridge" \
    -e PYTHONPATH=/app \
    -e ORCHESTRATOR_PORT="${ORCHESTRATOR_CONTAINER_PORT}" \
    -e PATH=/var/home/sanya/.npm-packages/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin:/app/.local/bin \
    -e NODE_PATH=/var/home/sanya/.npm-packages/lib/node_modules \
    -e TESTING=false \
    -e AI_BRIDGE_AUTOSTART_LOCAL_LLM=false \
    -e AI_BRIDGE_AUTOSTART_EASY_DIFFUSION=false \
    -e AI_BRIDGE_LOCAL_LLM_AUTO_PROVISION=false \
    -e AI_BRIDGE_LOCAL_LLM_ENDPOINT="http://host.containers.internal:${OLLAMA_PORT}" \
    -e AI_BRIDGE_LOCAL_LLM_PORT="${OLLAMA_PORT}" \
    -e AI_BRIDGE_LOCAL_LLM_MODEL="${LOCAL_MODEL}" \
    -e AI_KERNEL_ENABLED="${AI_KERNEL_ENABLED:-true}" \
    -e AI_KERNEL_BASE_URL="${AI_KERNEL_BASE_URL:-http://host.containers.internal:8012/v1}" \
    -e AI_KERNEL_API_KEY="${AI_KERNEL_API_KEY:-local}" \
    -e AI_KERNEL_MODEL_ALIAS="${AI_KERNEL_MODEL_ALIAS:-hauhaucs-qwen36-35b-a3b-aggressive:q4_k_m}" \
    -e AI_KERNEL_TCP_PROBE_HOSTS="${AI_KERNEL_TCP_PROBE_HOSTS:-host.containers.internal:8012}" \
    -e AI_BRIDGE_AI_KERNEL_MANAGE_REMOTE="${AI_BRIDGE_AI_KERNEL_MANAGE_REMOTE:-false}" \
    -e AI_BRIDGE_HOST_WORKSPACE_ROOT="$PROJECT_ROOT" \
    -e AI_KERNEL_HOST_HOME="$HOME" \
    -e AI_BRIDGE_WORKSPACE_ROOT=/workspace \
    -e AI_BRIDGE_EASY_DIFFUSION_START_ENABLED=false \
    -e AI_BRIDGE_MEMORY_ENABLED=true \
    -e AI_BRIDGE_MEMORY_DATABASE_URL="postgresql+psycopg2://ai_bridge:ai_bridge_password@host.containers.internal:5432/ai_bridge" \
    -e AI_BRIDGE_RABBITMQ_URL="amqp://guest:guest@host.containers.internal:5672/" \
    -e AI_BRIDGE_MESSAGE_BUS_BACKEND=rabbitmq \
    -e AI_BRIDGE_MEMORY_STORE_DIR=/app/memory_store \
    -e AI_BRIDGE_LIVE_MODEL_PROBE=false \
    -e AI_BRIDGE_REQUIRE_EXTERNAL_SCANNERS=false \
    -e AI_BRIDGE_DISABLE_SOURCECRAFT="${AI_BRIDGE_DISABLE_SOURCECRAFT:-false}" \
    -e AI_BRIDGE_ENABLE_VOICE=false \
    -e AI_BRIDGE_AUTO_APPROVE=true \
    -e AI_BRIDGE_CONFIRMATION_POLICY=full_auto \
    -e OPENAI_SESSION_TOKEN_BUDGET=120000 \
    -v "${MEMORY_VOLUME_NAME}:/app/memory_store" \
    -v "/var/home/sanya/.npm-packages:/var/home/sanya/.npm-packages:ro,z" \
    -v "$PROJECT_ROOT/.env.bridge:/app/.env.bridge:ro,z" \
    -v "$PROJECT_ROOT/scripts:/app/scripts:ro,z" \
    -v "$PROJECT_ROOT/.tooling:/app/.tooling:ro,z" \
    -v "$PROJECT_ROOT:/workspace:z" \
    "$ORCHESTRATOR_IMAGE" >/dev/null

  wait_for_http "http://127.0.0.1:${ORCHESTRATOR_PORT}${ORCHESTRATOR_HEALTH_PATH}" "$ORCHESTRATOR_READY_ATTEMPTS" "$ORCHESTRATOR_READY_SLEEP_SEC" || {
    host_run podman logs --tail 120 "$ORCHESTRATOR_CONTAINER" >&2 || true
    echo "[ERROR] Orchestrator health endpoint did not become ready: ${ORCHESTRATOR_HEALTH_PATH} on port ${ORCHESTRATOR_PORT}." >&2
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

verify_ai_kernel() {
  if [ "$RUN_AI_KERNEL" -eq 0 ]; then
    warn "AI Kernel startup/check skipped by flag."
    return
  fi

  log "Verifying AI Kernel connectivity..."
  host_run podman exec "$ORCHESTRATOR_CONTAINER" python -m core.scripts.verify_provider_stack || warn "AI Kernel/provider verification reported issues."
}

launch_post_start_verifications() {
  local verify_log="${AI_BRIDGE_BOOTSTRAP_VERIFY_LOG:-/tmp/hebrew-bootstrap-poststart.log}"
  (
    trap '' HUP
    verify_ai_kernel
    verify_mistral
    verify_agy
  ) >"$verify_log" 2>&1 &
  local verify_pid=$!
  disown "$verify_pid" 2>/dev/null || true
  log "Post-start verification moved to background: pid=${verify_pid} log=${verify_log}"
}

verify_agy() {
  if ! host_run bash -lc 'command -v agy >/dev/null 2>&1 || command -v antigravity >/dev/null 2>&1' >/dev/null 2>&1; then
    warn "No Antigravity-compatible CLI is installed on the host. Antigravity provider will stay degraded until agy/antigravity is installed or API mode is fixed."
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
  if [ "$RUN_AI_KERNEL" -eq 1 ]; then
    start_ai_kernel
  fi

  start_postgres
  start_rabbitmq
  build_orchestrator_image
  start_orchestrator
  launch_post_start_verifications
  print_summary
}

main "$@"
