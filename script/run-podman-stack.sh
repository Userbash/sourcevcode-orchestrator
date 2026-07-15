#!/bin/sh

set -eu

ROOT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
GO_CORE_DIR="$ROOT_DIR/go-core"
AI_KERNEL_PROXY_DIR="$ROOT_DIR/script/ai-kernel-proxy"

NETWORK_NAME="${NETWORK_NAME:-hebrew-net}"

POSTGRES_CONTAINER="${POSTGRES_CONTAINER:-ai_bridge_db}"
RABBITMQ_CONTAINER="${RABBITMQ_CONTAINER:-ai_bridge_rabbitmq}"
LOCAL_LLM_CONTAINER="${LOCAL_LLM_CONTAINER:-ai_bridge_local_llm}"
AI_KERNEL_CONTAINER="${AI_KERNEL_CONTAINER:-ai_bridge_ai_kernel}"
GO_CORE_CONTAINER="${GO_CORE_CONTAINER:-go_core}"

POSTGRES_VOLUME="${POSTGRES_VOLUME:-hebrew_pg_data}"
RABBITMQ_VOLUME="${RABBITMQ_VOLUME:-f4e8a2ce6ed671173eddf888afcefc9489463ff48774ed94a56938d48b86a215}"
LOCAL_LLM_VOLUME="${LOCAL_LLM_VOLUME:-hebrew_ollama_data}"
AI_KERNEL_VOLUME="${AI_KERNEL_VOLUME:-ai_kernel_models}"
GO_CORE_VOLUME="${GO_CORE_VOLUME:-hebrew_core_memory}"

POSTGRES_USER="${POSTGRES_USER:-ai_bridge}"
POSTGRES_PASSWORD="${POSTGRES_PASSWORD:-ai_bridge_password}"
POSTGRES_DB="${POSTGRES_DB:-ai_bridge}"

RABBITMQ_USER="${RABBITMQ_USER:-guest}"
RABBITMQ_PASSWORD="${RABBITMQ_PASSWORD:-guest}"

AI_KERNEL_API_KEY="${AI_KERNEL_API_KEY:-local}"
AI_KERNEL_MODE="${AI_KERNEL_MODE:-proxy-host}"
AI_KERNEL_UPSTREAM="${AI_KERNEL_UPSTREAM:-http://host.containers.internal:8012}"
AI_KERNEL_UPSTREAM_CHECK="${AI_KERNEL_UPSTREAM_CHECK:-http://127.0.0.1:8012}"
AI_KERNEL_HOST_PORT="${AI_KERNEL_HOST_PORT:-}"

GO_CORE_IMAGE="${GO_CORE_IMAGE:-localhost/go-core:local}"
POSTGRES_IMAGE="${POSTGRES_IMAGE:-docker.io/pgvector/pgvector:pg16}"
RABBITMQ_IMAGE="${RABBITMQ_IMAGE:-docker.io/library/rabbitmq:3-management}"
LOCAL_LLM_IMAGE="${LOCAL_LLM_IMAGE:-docker.io/ollama/ollama:latest}"
AI_KERNEL_IMAGE="${AI_KERNEL_IMAGE:-localhost/hebrew-ai-kernel:local}"
AI_KERNEL_PROXY_IMAGE="${AI_KERNEL_PROXY_IMAGE:-localhost/ai-kernel-proxy:local}"

choose_podman() {
    if command -v podman >/dev/null 2>&1; then
        PODMAN_BIN="podman"
        return
    fi

    if command -v flatpak-spawn >/dev/null 2>&1 && flatpak-spawn --host which podman >/dev/null 2>&1; then
        PODMAN_BIN="flatpak-spawn --host podman"
        return
    fi

    echo "podman not found" >&2
    exit 1
}

run_podman() {
    # shellcheck disable=SC2086
    $PODMAN_BIN "$@"
}

require_cmd() {
    if ! command -v "$1" >/dev/null 2>&1; then
        echo "required command not found: $1" >&2
        exit 1
    fi
}

ensure_network() {
    if ! run_podman network exists "$NETWORK_NAME"; then
        run_podman network create "$NETWORK_NAME"
    fi
}

ensure_volume() {
    volume_name=$1
    if ! run_podman volume exists "$volume_name"; then
        run_podman volume create "$volume_name"
    fi
}

container_exists() {
    run_podman container exists "$1"
}

remove_container_if_exists() {
    if container_exists "$1"; then
        run_podman rm -f "$1"
    fi
}

build_go_core() {
    run_podman build -t "$GO_CORE_IMAGE" "$GO_CORE_DIR"
}

build_ai_kernel() {
    case "$AI_KERNEL_MODE" in
        proxy-host)
            run_podman build -t "$AI_KERNEL_PROXY_IMAGE" "$AI_KERNEL_PROXY_DIR"
            ;;
        real)
            if ! run_podman image exists "$AI_KERNEL_IMAGE"; then
                echo "ai_kernel image not found: $AI_KERNEL_IMAGE" >&2
                echo "The original ai_kernel build context was removed from the repository." >&2
                exit 1
            fi
            ;;
        *)
            echo "unsupported AI_KERNEL_MODE: $AI_KERNEL_MODE" >&2
            exit 1
            ;;
    esac
}

wait_for_http() {
    name=$1
    url=$2
    attempts=${3:-60}
    delay=${4:-2}
    i=1
    while [ "$i" -le "$attempts" ]; do
        if curl -fsS "$url" >/dev/null 2>&1; then
            return 0
        fi
        sleep "$delay"
        i=$((i + 1))
    done
    echo "timeout waiting for $name at $url" >&2
    return 1
}

check_ai_kernel_upstream() {
    curl -fsS \
        -H "Authorization: Bearer $AI_KERNEL_API_KEY" \
        "$AI_KERNEL_UPSTREAM_CHECK/v1/models" >/dev/null
}

run_postgresql() {
    ensure_volume "$POSTGRES_VOLUME"
    remove_container_if_exists "$POSTGRES_CONTAINER"
    run_podman run -d \
        --name "$POSTGRES_CONTAINER" \
        --network "$NETWORK_NAME" \
        --network-alias postgresql \
        -p 5432:5432 \
        -e POSTGRES_USER="$POSTGRES_USER" \
        -e POSTGRES_PASSWORD="$POSTGRES_PASSWORD" \
        -e POSTGRES_DB="$POSTGRES_DB" \
        -e PGDATA=/var/lib/postgresql/data \
        -v "$POSTGRES_VOLUME":/var/lib/postgresql/data \
        "$POSTGRES_IMAGE" \
        postgres \
        -c shared_buffers=256MB \
        -c max_connections=200 \
        -c effective_cache_size=768MB \
        -c work_mem=16MB \
        -c wal_level=logical \
        -c track_commit_timestamp=on
}

run_rabbitmq() {
    ensure_volume "$RABBITMQ_VOLUME"
    remove_container_if_exists "$RABBITMQ_CONTAINER"
    run_podman run -d \
        --name "$RABBITMQ_CONTAINER" \
        --network "$NETWORK_NAME" \
        --network-alias rabbitmq \
        -p 5672:5672 \
        -p 15672:15672 \
        -e RABBITMQ_DEFAULT_USER="$RABBITMQ_USER" \
        -e RABBITMQ_DEFAULT_PASS="$RABBITMQ_PASSWORD" \
        -e RABBITMQ_DATA_DIR=/var/lib/rabbitmq \
        -v "$RABBITMQ_VOLUME":/var/lib/rabbitmq \
        "$RABBITMQ_IMAGE"
}

run_local_llm() {
    ensure_volume "$LOCAL_LLM_VOLUME"
    remove_container_if_exists "$LOCAL_LLM_CONTAINER"
    run_podman run -d \
        --name "$LOCAL_LLM_CONTAINER" \
        --network "$NETWORK_NAME" \
        --network-alias local_llm \
        --security-opt label=disable \
        --device /dev/nvidia-uvm \
        --device /dev/nvidia-uvm-tools \
        --device /dev/nvidiactl \
        --device /dev/nvidia0 \
        --device /dev/dri/card2 \
        --device /dev/dri/renderD129 \
        -p 11434:11434 \
        -e OLLAMA_HOST=0.0.0.0:11434 \
        -e NVIDIA_DRIVER_CAPABILITIES=compute,utility \
        -e NVIDIA_VISIBLE_DEVICES=all \
        -v "$LOCAL_LLM_VOLUME":/root/.ollama \
        "$LOCAL_LLM_IMAGE" \
        serve
}

run_ai_kernel() {
    ai_kernel_port_args=""
    if [ -n "$AI_KERNEL_HOST_PORT" ]; then
        ai_kernel_port_args="-p $AI_KERNEL_HOST_PORT:8012"
    fi
    remove_container_if_exists "$AI_KERNEL_CONTAINER"
    case "$AI_KERNEL_MODE" in
        proxy-host)
            run_podman run -d \
                --name "$AI_KERNEL_CONTAINER" \
                --network "$NETWORK_NAME" \
                --network-alias ai_kernel \
                $ai_kernel_port_args \
                -e LISTEN_ADDR=:8012 \
                -e UPSTREAM_BASE_URL="$AI_KERNEL_UPSTREAM" \
                -e UPSTREAM_API_KEY="$AI_KERNEL_API_KEY" \
                "$AI_KERNEL_PROXY_IMAGE"
            ;;
        real)
            ensure_volume "$AI_KERNEL_VOLUME"
            run_podman run -d \
                --name "$AI_KERNEL_CONTAINER" \
                --network "$NETWORK_NAME" \
                --network-alias ai_kernel \
                $ai_kernel_port_args \
                -e AI_KERNEL_HOST=0.0.0.0 \
                -e AI_KERNEL_PORT=8012 \
                -e AI_KERNEL_API_KEY="$AI_KERNEL_API_KEY" \
                -v "$AI_KERNEL_VOLUME":/models \
                "$AI_KERNEL_IMAGE"
            ;;
    esac
}

run_go_core() {
    ensure_volume "$GO_CORE_VOLUME"
    remove_container_if_exists "$GO_CORE_CONTAINER"
    run_podman run -d \
        --name "$GO_CORE_CONTAINER" \
        --network "$NETWORK_NAME" \
        --network-alias go_core \
        -p 8010:8010 \
        --env-file "$ROOT_DIR/.env" \
        --env-file "$ROOT_DIR/.env.bridge" \
        -e ORCHESTRATOR_HOST=0.0.0.0 \
        -e ORCHESTRATOR_PORT=8010 \
        -e AI_BRIDGE_API_HOST=0.0.0.0 \
        -e AI_BRIDGE_API_PORT=8010 \
        -e AI_BRIDGE_MEMORY_DATABASE_URL="postgresql://$POSTGRES_USER:$POSTGRES_PASSWORD@postgresql:5432/$POSTGRES_DB?sslmode=disable" \
        -e AI_BRIDGE_RABBITMQ_URL="amqp://$RABBITMQ_USER:$RABBITMQ_PASSWORD@rabbitmq:5672/" \
        -e AI_BRIDGE_LOCAL_LLM_ENDPOINT=http://local_llm:11434 \
        -e AI_BRIDGE_LOCAL_LLM_MODELS_ENDPOINT=http://local_llm:11434/api/tags \
        -e AI_BRIDGE_LOCAL_LLM_CHAT_COMPLETIONS_ENDPOINT=http://local_llm:11434/v1/chat/completions \
        -e AI_KERNEL_BASE_URL=http://ai_kernel:8012/v1 \
        -e AI_BRIDGE_AI_KERNEL_BASE_URL=http://ai_kernel:8012/v1 \
        -e AI_BRIDGE_AI_KERNEL_MODELS_ENDPOINT=http://ai_kernel:8012/v1/models \
        -e AI_BRIDGE_AI_KERNEL_CHAT_COMPLETIONS_ENDPOINT=http://ai_kernel:8012/v1/chat/completions \
        -e AI_KERNEL_API_KEY="$AI_KERNEL_API_KEY" \
        -v "$GO_CORE_VOLUME":/app/db_backups \
        "$GO_CORE_IMAGE"
}

up() {
    require_cmd curl
    ensure_network
    build_ai_kernel
    build_go_core
    if [ "$AI_KERNEL_MODE" = "proxy-host" ]; then
        check_ai_kernel_upstream
    fi
    run_postgresql
    run_rabbitmq
    run_local_llm
    run_ai_kernel
    run_go_core
    wait_for_http go_core http://127.0.0.1:8010/health/full 90 2
}

down() {
    remove_container_if_exists "$GO_CORE_CONTAINER"
    remove_container_if_exists "$AI_KERNEL_CONTAINER"
    remove_container_if_exists "$LOCAL_LLM_CONTAINER"
    remove_container_if_exists "$RABBITMQ_CONTAINER"
    remove_container_if_exists "$POSTGRES_CONTAINER"
}

restart() {
    down
    up
}

rebuild() {
    build_ai_kernel
    build_go_core
}

logs() {
    if [ "${2:-}" = "" ]; then
        echo "usage: $0 logs <container>" >&2
        exit 1
    fi
    run_podman logs -f "$2"
}

ps() {
    run_podman ps -a --filter network="$NETWORK_NAME"
}

diagnose() {
    require_cmd curl
    echo "== podman binary =="
    echo "$PODMAN_BIN"
    echo
    echo "== containers =="
    run_podman ps -a --format '{{.Names}}|{{.Image}}|{{.Status}}|{{.Ports}}'
    echo
    echo "== network =="
    run_podman network inspect "$NETWORK_NAME"
    echo
    echo "== orchestrator health =="
    curl -sS http://127.0.0.1:8010/health/full
    echo
}

print_binary() {
    echo "$PODMAN_BIN"
}

usage() {
    cat <<'USAGE'
Usage:
  script/run-podman-stack.sh up
  script/run-podman-stack.sh down
  script/run-podman-stack.sh restart
  script/run-podman-stack.sh rebuild
  script/run-podman-stack.sh diagnose
  script/run-podman-stack.sh ps
  script/run-podman-stack.sh logs <container>
  script/run-podman-stack.sh print-binary

Containers:
  postgresql -> ai_bridge_db
  rabbitmq   -> ai_bridge_rabbitmq
  local_llm  -> ai_bridge_local_llm
  ai_kernel  -> ai_bridge_ai_kernel
  go_core    -> go_core
USAGE
}

choose_podman

case "${1:-}" in
    up)
        up
        ;;
    down)
        down
        ;;
    restart)
        restart
        ;;
    rebuild)
        rebuild
        ;;
    diagnose)
        diagnose
        ;;
    ps)
        ps
        ;;
    logs)
        logs "$@"
        ;;
    print-binary)
        print_binary
        ;;
    *)
        usage
        exit 1
        ;;
esac
