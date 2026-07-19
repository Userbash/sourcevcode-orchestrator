#!/bin/sh

set -eu

ROOT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
GO_CORE_DIR="$ROOT_DIR/go-core"
AI_KERNEL_PROXY_DIR="$ROOT_DIR/scripts/ai-kernel-proxy"
STATE_DIR="${STATE_DIR:-$ROOT_DIR/.runtime}"
GO_CORE_IMAGE_STATE_FILE="$STATE_DIR/go-core-image.ref"
GO_CORE_HOST_PORT_STATE_FILE="$STATE_DIR/go-core-host-port"

NETWORK_NAME="${NETWORK_NAME:-hebrew-net}"

POSTGRES_CONTAINER="${POSTGRES_CONTAINER:-ai_bridge_db}"
RABBITMQ_CONTAINER="${RABBITMQ_CONTAINER:-ai_bridge_rabbitmq}"
AI_KERNEL_CONTAINER="${AI_KERNEL_CONTAINER:-ai_bridge_ai_kernel}"
GO_CORE_CONTAINER="${GO_CORE_CONTAINER:-go_core}"
LOKI_CONTAINER="${LOKI_CONTAINER:-ai_bridge_loki}"
PROMTAIL_CONTAINER="${PROMTAIL_CONTAINER:-ai_bridge_promtail}"
PROMETHEUS_CONTAINER="${PROMETHEUS_CONTAINER:-ai_bridge_prometheus}"
GRAFANA_CONTAINER="${GRAFANA_CONTAINER:-ai_bridge_grafana}"

POSTGRES_VOLUME="${POSTGRES_VOLUME:-hebrew_pg_data}"
RABBITMQ_VOLUME="${RABBITMQ_VOLUME:-f4e8a2ce6ed671173eddf888afcefc9489463ff48774ed94a56938d48b86a215}"
AI_KERNEL_VOLUME="${AI_KERNEL_VOLUME:-ai_kernel_models}"
GO_CORE_VOLUME="${GO_CORE_VOLUME:-hebrew_core_memory}"
LOKI_VOLUME="${LOKI_VOLUME:-ai_bridge_loki_data}"
PROMTAIL_VOLUME="${PROMTAIL_VOLUME:-ai_bridge_promtail_data}"
PROMETHEUS_VOLUME="${PROMETHEUS_VOLUME:-ai_bridge_prometheus_data}"
GRAFANA_VOLUME="${GRAFANA_VOLUME:-ai_bridge_grafana_data}"

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

GO_CORE_REPOSITORY="${GO_CORE_REPOSITORY:-localhost/go-core}"
GO_CORE_IMAGE="${GO_CORE_IMAGE:-}"
GO_CORE_VERSION="${GO_CORE_VERSION:-}"
GO_CORE_CONTAINER_PORT="${GO_CORE_CONTAINER_PORT:-8010}"
if [ "${GO_CORE_HOST_PORT+x}" = "x" ]; then
    GO_CORE_HOST_PORT_EXPLICIT=1
else
    GO_CORE_HOST_PORT_EXPLICIT=0
fi
GO_CORE_HOST_PORT="${GO_CORE_HOST_PORT:-$GO_CORE_CONTAINER_PORT}"
POSTGRES_IMAGE="${POSTGRES_IMAGE:-docker.io/pgvector/pgvector:pg16}"
RABBITMQ_IMAGE="${RABBITMQ_IMAGE:-docker.io/library/rabbitmq:3-management}"
AI_KERNEL_IMAGE="${AI_KERNEL_IMAGE:-localhost/hebrew-ai-kernel:local}"
AI_KERNEL_PROXY_IMAGE="${AI_KERNEL_PROXY_IMAGE:-localhost/ai-kernel-proxy:local}"
LOKI_IMAGE="${LOKI_IMAGE:-docker.io/grafana/loki:3.0.0}"
PROMTAIL_IMAGE="${PROMTAIL_IMAGE:-docker.io/grafana/promtail:3.0.0}"
PROMETHEUS_IMAGE="${PROMETHEUS_IMAGE:-docker.io/prom/prometheus:v2.54.1}"
GRAFANA_IMAGE="${GRAFANA_IMAGE:-localhost/ai-bridge-grafana:11.1.0}"

GO_CORE_CPU_LIMIT="${GO_CORE_CPU_LIMIT:-1.5}"
GO_CORE_MEMORY_LIMIT="${GO_CORE_MEMORY_LIMIT:-1024m}"
GO_CORE_MEMORY_RESERVATION="${GO_CORE_MEMORY_RESERVATION:-512m}"
GO_CORE_PIDS_LIMIT="${GO_CORE_PIDS_LIMIT:-256}"
GO_CORE_GOMAXPROCS="${GO_CORE_GOMAXPROCS:-1}"
GO_CORE_GOMEMLIMIT="${GO_CORE_GOMEMLIMIT:-640MiB}"
GO_CORE_GOGC="${GO_CORE_GOGC:-50}"
GO_CORE_BOOTSTRAP_SAFE_MODE="${GO_CORE_BOOTSTRAP_SAFE_MODE:-true}"
GO_CORE_PG_SKIP_SCHEMA_ENSURE="${GO_CORE_PG_SKIP_SCHEMA_ENSURE:-true}"
GO_CORE_PG_SKIP_VECTOR_INDEXES="${GO_CORE_PG_SKIP_VECTOR_INDEXES:-true}"
GO_CORE_PG_SKIP_VECTOR_EXTENSION="${GO_CORE_PG_SKIP_VECTOR_EXTENSION:-true}"
GO_CORE_PG_MAX_OPEN_CONNS="${GO_CORE_PG_MAX_OPEN_CONNS:-2}"
GO_CORE_PG_MAX_IDLE_CONNS="${GO_CORE_PG_MAX_IDLE_CONNS:-1}"
GO_CORE_PG_SCHEMA_TIMEOUT="${GO_CORE_PG_SCHEMA_TIMEOUT:-20s}"
GO_CORE_PROVIDER_HEALTH_WORKERS="${GO_CORE_PROVIDER_HEALTH_WORKERS:-1}"
GO_CORE_PROVIDER_HEALTH_QUEUE_SIZE="${GO_CORE_PROVIDER_HEALTH_QUEUE_SIZE:-2}"
GO_CORE_CPU_RESERVE="${GO_CORE_CPU_RESERVE:-1}"
GO_CORE_MAX_PARALLELISM="${GO_CORE_MAX_PARALLELISM:-1}"
GO_CORE_MAX_CONCURRENT_TASKS="${GO_CORE_MAX_CONCURRENT_TASKS:-1}"
GO_CORE_MAX_CONCURRENT_PER_AGENT="${GO_CORE_MAX_CONCURRENT_PER_AGENT:-1}"
GO_CORE_MAX_CONCURRENT_PER_MODEL="${GO_CORE_MAX_CONCURRENT_PER_MODEL:-1}"
GO_CORE_SUBMIT_WORKERS="${GO_CORE_SUBMIT_WORKERS:-1}"
GO_CORE_RESULT_WORKERS="${GO_CORE_RESULT_WORKERS:-1}"
GO_CORE_AGENT_WORKERS="${GO_CORE_AGENT_WORKERS:-1}"
GO_CORE_PROVIDER_MAX_CONCURRENT_PER_KEY="${GO_CORE_PROVIDER_MAX_CONCURRENT_PER_KEY:-1}"
GO_CORE_PROVIDER_MAX_CONCURRENT_PER_MODEL="${GO_CORE_PROVIDER_MAX_CONCURRENT_PER_MODEL:-1}"
GO_CORE_SELFLEARN_ENABLED="${GO_CORE_SELFLEARN_ENABLED:-false}"
GO_CORE_CODING_RUNTIME_ENABLED="${GO_CORE_CODING_RUNTIME_ENABLED:-false}"
GO_CORE_SUBMIT_MODE="${GO_CORE_SUBMIT_MODE:-sync}"
GO_CORE_MESSAGE_BUS_BACKEND="${GO_CORE_MESSAGE_BUS_BACKEND:-memory}"
GO_CORE_LOG_BUFFER_SIZE="${GO_CORE_LOG_BUFFER_SIZE:-64}"
AI_BRIDGE_MEMORY_ENABLED="${AI_BRIDGE_MEMORY_ENABLED:-false}"

LOKI_CPU_LIMIT="${LOKI_CPU_LIMIT:-1}"
LOKI_MEMORY_LIMIT="${LOKI_MEMORY_LIMIT:-512m}"
LOKI_MEMORY_RESERVATION="${LOKI_MEMORY_RESERVATION:-256m}"
LOKI_PIDS_LIMIT="${LOKI_PIDS_LIMIT:-128}"

PROMTAIL_CPU_LIMIT="${PROMTAIL_CPU_LIMIT:-0.75}"
PROMTAIL_MEMORY_LIMIT="${PROMTAIL_MEMORY_LIMIT:-256m}"
PROMTAIL_MEMORY_RESERVATION="${PROMTAIL_MEMORY_RESERVATION:-128m}"
PROMTAIL_PIDS_LIMIT="${PROMTAIL_PIDS_LIMIT:-96}"

PROMETHEUS_CPU_LIMIT="${PROMETHEUS_CPU_LIMIT:-1}"
PROMETHEUS_MEMORY_LIMIT="${PROMETHEUS_MEMORY_LIMIT:-512m}"
PROMETHEUS_MEMORY_RESERVATION="${PROMETHEUS_MEMORY_RESERVATION:-256m}"
PROMETHEUS_PIDS_LIMIT="${PROMETHEUS_PIDS_LIMIT:-128}"

GRAFANA_CPU_LIMIT="${GRAFANA_CPU_LIMIT:-1}"
GRAFANA_MEMORY_LIMIT="${GRAFANA_MEMORY_LIMIT:-512m}"
GRAFANA_MEMORY_RESERVATION="${GRAFANA_MEMORY_RESERVATION:-256m}"
GRAFANA_PIDS_LIMIT="${GRAFANA_PIDS_LIMIT:-128}"

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

ensure_state_dir() {
    mkdir -p "$STATE_DIR"
}

git_short_commit() {
    git -C "$ROOT_DIR" rev-parse --short=12 HEAD
}

git_full_commit() {
    git -C "$ROOT_DIR" rev-parse HEAD
}

utc_build_time() {
    date -u +%Y-%m-%dT%H:%M:%SZ
}

default_go_core_version() {
    commit=$(git_short_commit)
    timestamp=$(date -u +%Y.%m.%d-%H%M%S)
    printf '%s-%s\n' "$timestamp" "$commit"
}

write_go_core_target_image() {
    ensure_state_dir
    printf '%s\n' "$1" >"$GO_CORE_IMAGE_STATE_FILE"
}

write_go_core_host_port() {
    ensure_state_dir
    printf '%s\n' "$1" >"$GO_CORE_HOST_PORT_STATE_FILE"
}

read_go_core_target_image() {
    if [ -n "$GO_CORE_IMAGE" ]; then
        printf '%s\n' "$GO_CORE_IMAGE"
        return
    fi
    if [ -f "$GO_CORE_IMAGE_STATE_FILE" ]; then
        cat "$GO_CORE_IMAGE_STATE_FILE"
        return
    fi
    printf '%s:current\n' "$GO_CORE_REPOSITORY"
}

read_go_core_target_version() {
    image_ref=$(read_go_core_target_image)
    printf '%s\n' "${image_ref##*:}"
}

read_go_core_host_port() {
    if [ -f "$GO_CORE_HOST_PORT_STATE_FILE" ]; then
        cat "$GO_CORE_HOST_PORT_STATE_FILE"
        return 0
    fi
    return 1
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

container_host_port() {
    if ! container_exists "$GO_CORE_CONTAINER"; then
        return 1
    fi
    mapping=$(run_podman port "$GO_CORE_CONTAINER" "$GO_CORE_CONTAINER_PORT/tcp" 2>/dev/null | awk 'NR==1 { print; exit }')
    if [ -z "$mapping" ]; then
        return 1
    fi
    printf '%s\n' "${mapping##*:}"
}

resolve_go_core_host_port() {
    if port=$(container_host_port 2>/dev/null); then
        printf '%s\n' "$port"
        return 0
    fi
    if [ "$GO_CORE_HOST_PORT" = "auto" ]; then
        if port=$(read_go_core_host_port 2>/dev/null); then
            printf '%s\n' "$port"
            return 0
        fi
        printf '%s\n' "$GO_CORE_CONTAINER_PORT"
        return 0
    fi
    if [ "$GO_CORE_HOST_PORT_EXPLICIT" -eq 1 ]; then
        printf '%s\n' "$GO_CORE_HOST_PORT"
        return 0
    fi
    printf '%s\n' "$GO_CORE_CONTAINER_PORT"
}

build_go_core() {
    require_cmd git
    require_cmd date
    version="$GO_CORE_VERSION"
    if [ -z "$version" ]; then
        version=$(default_go_core_version)
    fi
    commit=$(git_short_commit)
    build_time=$(utc_build_time)
    image_ref="${GO_CORE_REPOSITORY}:$version"
    run_podman build \
        --build-arg VERSION="$version" \
        --build-arg COMMIT="$commit" \
        --build-arg BUILD_TIME="$build_time" \
        -t "$image_ref" \
        -t "${GO_CORE_REPOSITORY}:current" \
        "$GO_CORE_DIR"
    write_go_core_target_image "$image_ref"
    printf 'go_core image built: %s\n' "$image_ref"
}

prepare_go_core_image() {
    if [ -n "$GO_CORE_IMAGE" ]; then
        if ! run_podman image exists "$GO_CORE_IMAGE"; then
            echo "go_core image not found: $GO_CORE_IMAGE" >&2
            exit 1
        fi
        write_go_core_target_image "$GO_CORE_IMAGE"
        printf 'go_core image selected: %s\n' "$GO_CORE_IMAGE"
        return
    fi
    build_go_core
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

build_grafana() {
    run_podman build \
        -t "$GRAFANA_IMAGE" \
        "$ROOT_DIR/infra/grafana"
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
        -v "$ROOT_DIR/infra/rabbitmq/rabbitmq.conf":/etc/rabbitmq/rabbitmq.conf:ro,Z \
        -v "$ROOT_DIR/infra/rabbitmq/enabled_plugins":/etc/rabbitmq/enabled_plugins:ro,Z \
        -v "$RABBITMQ_VOLUME":/var/lib/rabbitmq \
        "$RABBITMQ_IMAGE"
}

run_ai_kernel() {
    ai_kernel_port_args=
    if [ -n "$AI_KERNEL_HOST_PORT" ]; then
        ai_kernel_port_args="-p $AI_KERNEL_HOST_PORT:8012"
    fi
    remove_container_if_exists "$AI_KERNEL_CONTAINER"
    case "$AI_KERNEL_MODE" in
        proxy-host)
            # shellcheck disable=SC2086
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
            # shellcheck disable=SC2086
            run_podman run -d \
                --name "$AI_KERNEL_CONTAINER" \
                --network "$NETWORK_NAME" \
                --network-alias ai_kernel \
                $ai_kernel_port_args \
                -e AI_KERNEL_HOST=0.0.0.0 \
                -e AI_KERNEL_PORT=8012 \
                -e AI_KERNEL_REQUIRE_API_KEY=false \
                -v "$AI_KERNEL_VOLUME":/models \
                "$AI_KERNEL_IMAGE"
            ;;
    esac
}

run_loki() {
    ensure_volume "$LOKI_VOLUME"
    remove_container_if_exists "$LOKI_CONTAINER"
    run_podman run -d \
        --name "$LOKI_CONTAINER" \
        --restart unless-stopped \
        --network "$NETWORK_NAME" \
        --network-alias loki \
        --cpus "$LOKI_CPU_LIMIT" \
        --memory "$LOKI_MEMORY_LIMIT" \
        --memory-reservation "$LOKI_MEMORY_RESERVATION" \
        --pids-limit "$LOKI_PIDS_LIMIT" \
        -p 3100:3100 \
        -v "$ROOT_DIR/infra/loki/loki-config.yaml":/etc/loki/local-config.yaml:ro,Z \
        -v "$LOKI_VOLUME":/loki \
        "$LOKI_IMAGE" \
        -config.file=/etc/loki/local-config.yaml
}

run_promtail() {
    mkdir -p "$ROOT_DIR/.runtime/logs"
    ensure_volume "$PROMTAIL_VOLUME"
    remove_container_if_exists "$PROMTAIL_CONTAINER"
    run_podman run -d \
        --name "$PROMTAIL_CONTAINER" \
        --restart unless-stopped \
        --network "$NETWORK_NAME" \
        --network-alias promtail \
        --cpus "$PROMTAIL_CPU_LIMIT" \
        --memory "$PROMTAIL_MEMORY_LIMIT" \
        --memory-reservation "$PROMTAIL_MEMORY_RESERVATION" \
        --pids-limit "$PROMTAIL_PIDS_LIMIT" \
        -v "$ROOT_DIR/infra/loki/promtail-config.yaml":/etc/promtail/config.yml:ro,Z \
        -v "$ROOT_DIR/.runtime/logs":/var/log/runtime:ro,Z \
        -v "$PROMTAIL_VOLUME":/tmp/promtail \
        "$PROMTAIL_IMAGE" \
        -config.file=/etc/promtail/config.yml
}

run_prometheus() {
    ensure_volume "$PROMETHEUS_VOLUME"
    remove_container_if_exists "$PROMETHEUS_CONTAINER"
    run_podman run -d \
        --name "$PROMETHEUS_CONTAINER" \
        --restart unless-stopped \
        --network "$NETWORK_NAME" \
        --network-alias prometheus \
        --cpus "$PROMETHEUS_CPU_LIMIT" \
        --memory "$PROMETHEUS_MEMORY_LIMIT" \
        --memory-reservation "$PROMETHEUS_MEMORY_RESERVATION" \
        --pids-limit "$PROMETHEUS_PIDS_LIMIT" \
        -p 9090:9090 \
        -v "$ROOT_DIR/infra/prometheus/prometheus.yml":/etc/prometheus/prometheus.yml:ro,Z \
        -v "$PROMETHEUS_VOLUME":/prometheus \
        "$PROMETHEUS_IMAGE" \
        --config.file=/etc/prometheus/prometheus.yml \
        --storage.tsdb.path=/prometheus \
        --web.enable-lifecycle
}

run_grafana() {
    ensure_volume "$GRAFANA_VOLUME"
    remove_container_if_exists "$GRAFANA_CONTAINER"
    run_podman run -d \
        --name "$GRAFANA_CONTAINER" \
        --restart unless-stopped \
        --network "$NETWORK_NAME" \
        --network-alias grafana \
        --cpus "$GRAFANA_CPU_LIMIT" \
        --memory "$GRAFANA_MEMORY_LIMIT" \
        --memory-reservation "$GRAFANA_MEMORY_RESERVATION" \
        --pids-limit "$GRAFANA_PIDS_LIMIT" \
        -p 3000:3000 \
        -e GF_SECURITY_ADMIN_USER=admin \
        -e GF_SECURITY_ADMIN_PASSWORD=admin \
        -e GF_AUTH_ANONYMOUS_ENABLED=true \
        -e GF_AUTH_ANONYMOUS_ORG_ROLE=Viewer \
        -v "$ROOT_DIR/infra/grafana/provisioning":/etc/grafana/provisioning:ro,Z \
        -v "$GRAFANA_VOLUME":/var/lib/grafana \
        "$GRAFANA_IMAGE"
}

run_go_core() {
    go_core_image=$(read_go_core_target_image)
    if [ "$GO_CORE_HOST_PORT" = "auto" ]; then
        host_port=$(resolve_go_core_host_port)
        allow_retry=1
    elif [ "$GO_CORE_HOST_PORT_EXPLICIT" -eq 1 ]; then
        host_port="$GO_CORE_HOST_PORT"
        allow_retry=0
    else
        host_port=$(resolve_go_core_host_port)
        allow_retry=1
    fi
    ensure_volume "$GO_CORE_VOLUME"
    mkdir -p "$ROOT_DIR/.runtime/logs/go_core"
    chmod 0777 "$ROOT_DIR/.runtime/logs/go_core"
    remove_container_if_exists "$GO_CORE_CONTAINER"
    while :; do
        set +e
        run_output=$(
            run_podman run -d \
                --name "$GO_CORE_CONTAINER" \
                --restart unless-stopped \
                --network "$NETWORK_NAME" \
                --network-alias go_core \
                --cpus "$GO_CORE_CPU_LIMIT" \
                --memory "$GO_CORE_MEMORY_LIMIT" \
                --memory-reservation "$GO_CORE_MEMORY_RESERVATION" \
                --pids-limit "$GO_CORE_PIDS_LIMIT" \
                -p "$host_port:$GO_CORE_CONTAINER_PORT" \
                --env-file "$ROOT_DIR/.env" \
                --env-file "$ROOT_DIR/.env.bridge" \
                -e ORCHESTRATOR_HOST=0.0.0.0 \
                -e ORCHESTRATOR_PORT="$GO_CORE_CONTAINER_PORT" \
                -e AI_BRIDGE_API_HOST=0.0.0.0 \
                -e AI_BRIDGE_API_PORT="$GO_CORE_CONTAINER_PORT" \
                -e AI_BRIDGE_MEMORY_DATABASE_URL="postgresql://$POSTGRES_USER:$POSTGRES_PASSWORD@postgresql:5432/$POSTGRES_DB?sslmode=disable" \
                -e AI_BRIDGE_RABBITMQ_URL="amqp://$RABBITMQ_USER:$RABBITMQ_PASSWORD@rabbitmq:5672/" \
                -e AI_KERNEL_BASE_URL=http://ai_kernel:8012/v1 \
                -e AI_BRIDGE_AI_KERNEL_BASE_URL=http://ai_kernel:8012/v1 \
                -e AI_BRIDGE_AI_KERNEL_MODELS_ENDPOINT=http://ai_kernel:8012/v1/models \
                -e AI_BRIDGE_AI_KERNEL_CHAT_COMPLETIONS_ENDPOINT=http://ai_kernel:8012/v1/chat/completions \
                -e GO_CORE_RAG_EMBEDDING_BASE_URL=http://ai_kernel:8012/v1 \
                -e AI_BRIDGE_AI_KERNEL_MANAGE_REMOTE=false \
                -e AI_BRIDGE_MODEL_REFRESH_ENABLED=false \
                -e AI_BRIDGE_MODEL_VALIDATE_MODELS=false \
                -e AI_BRIDGE_MODEL_REFRESH_INTERVAL=30m \
                -e AI_BRIDGE_MODEL_VALIDATE_LIMIT=1 \
                -e AI_BRIDGE_MODEL_RETRY_COOLDOWN=30m \
                -e GO_CORE_MODEL_REGISTRY_ENABLED=false \
                -e GO_CORE_MODEL_REGISTRY_REFRESH_INTERVAL=30m \
                -e GO_CORE_PROVIDER_HEALTH_WORKERS="$GO_CORE_PROVIDER_HEALTH_WORKERS" \
                -e GO_CORE_PROVIDER_HEALTH_QUEUE_SIZE="$GO_CORE_PROVIDER_HEALTH_QUEUE_SIZE" \
                -e GO_CORE_PROVIDER_HEALTH_TTL=10m \
                -e GO_CORE_PROVIDER_HEALTH_COOLDOWN=10m \
                -e GO_CORE_PROVIDER_HEALTH_RATE_LIMIT_COOLDOWN=30m \
                -e GO_CORE_BOOTSTRAP_SAFE_MODE="$GO_CORE_BOOTSTRAP_SAFE_MODE" \
                -e GO_CORE_PG_SKIP_SCHEMA_ENSURE="$GO_CORE_PG_SKIP_SCHEMA_ENSURE" \
                -e GO_CORE_PG_SKIP_VECTOR_INDEXES="$GO_CORE_PG_SKIP_VECTOR_INDEXES" \
                -e GO_CORE_PG_SKIP_VECTOR_EXTENSION="$GO_CORE_PG_SKIP_VECTOR_EXTENSION" \
                -e GO_CORE_PG_MAX_OPEN_CONNS="$GO_CORE_PG_MAX_OPEN_CONNS" \
                -e GO_CORE_PG_MAX_IDLE_CONNS="$GO_CORE_PG_MAX_IDLE_CONNS" \
                -e GO_CORE_PG_SCHEMA_TIMEOUT="$GO_CORE_PG_SCHEMA_TIMEOUT" \
                -e AI_BRIDGE_GOMAXPROCS="$GO_CORE_GOMAXPROCS" \
                -e AI_BRIDGE_CPU_RESERVE="$GO_CORE_CPU_RESERVE" \
                -e GO_CORE_MAX_PARALLELISM="$GO_CORE_MAX_PARALLELISM" \
                -e GO_CORE_MAX_CONCURRENT_TASKS="$GO_CORE_MAX_CONCURRENT_TASKS" \
                -e GO_CORE_MAX_CONCURRENT_PER_AGENT="$GO_CORE_MAX_CONCURRENT_PER_AGENT" \
                -e GO_CORE_MAX_CONCURRENT_PER_MODEL="$GO_CORE_MAX_CONCURRENT_PER_MODEL" \
                -e GO_CORE_SUBMIT_WORKERS="$GO_CORE_SUBMIT_WORKERS" \
                -e GO_CORE_RESULT_WORKERS="$GO_CORE_RESULT_WORKERS" \
                -e GO_CORE_AGENT_WORKERS="$GO_CORE_AGENT_WORKERS" \
                -e GO_CORE_PROVIDER_MAX_CONCURRENT_PER_KEY="$GO_CORE_PROVIDER_MAX_CONCURRENT_PER_KEY" \
                -e GO_CORE_PROVIDER_MAX_CONCURRENT_PER_MODEL="$GO_CORE_PROVIDER_MAX_CONCURRENT_PER_MODEL" \
                -e GO_CORE_SUBMIT_MODE="$GO_CORE_SUBMIT_MODE" \
                -e GO_CORE_MESSAGE_BUS_BACKEND="$GO_CORE_MESSAGE_BUS_BACKEND" \
                -e AI_BRIDGE_MESSAGE_BUS_BACKEND="$GO_CORE_MESSAGE_BUS_BACKEND" \
                -e GO_CORE_SELFLEARN_ENABLED="$GO_CORE_SELFLEARN_ENABLED" \
                -e GO_CORE_CODING_RUNTIME_ENABLED="$GO_CORE_CODING_RUNTIME_ENABLED" \
                -e AI_BRIDGE_MEMORY_ENABLED="$AI_BRIDGE_MEMORY_ENABLED" \
                -e GOMEMLIMIT="$GO_CORE_GOMEMLIMIT" \
                -e GOGC="$GO_CORE_GOGC" \
                -e GO_CORE_LOG_FORMAT=json \
                -e GO_CORE_LOG_LEVEL=info \
                -e GO_CORE_LOG_PATH=/var/log/go-core/orchestrator.log \
                -e GO_CORE_LOG_BUFFER_SIZE="$GO_CORE_LOG_BUFFER_SIZE" \
                -e AI_KERNEL_REQUIRE_API_KEY=false \
                -v "$GO_CORE_VOLUME":/app/db_backups \
                -v "$ROOT_DIR/.runtime/logs/go_core":/var/log/go-core:Z \
                "$go_core_image" 2>&1
        )
        run_status=$?
        set -e
        if [ "$run_status" -eq 0 ]; then
            write_go_core_host_port "$host_port"
            printf '%s\n' "$run_output"
            printf 'go_core host port: %s\n' "$host_port"
            return 0
        fi
        remove_container_if_exists "$GO_CORE_CONTAINER"
        case "$run_output" in
            *"address already in use"*)
                if [ "$allow_retry" -eq 1 ]; then
                    host_port=$((host_port + 1))
                    continue
                fi
                ;;
        esac
        printf '%s\n' "$run_output" >&2
        return "$run_status"
    done
}

up() {
    require_cmd curl
    ensure_network
    build_ai_kernel
    build_grafana
    prepare_go_core_image
    if [ "$AI_KERNEL_MODE" = "proxy-host" ]; then
        check_ai_kernel_upstream
    fi
    run_postgresql
    run_rabbitmq
    run_ai_kernel
    run_loki
    wait_for_http loki "http://127.0.0.1:3100/ready" 60 2
    run_promtail
    run_go_core
    go_core_host_port=$(resolve_go_core_host_port)
    go_core_ready=1
    if ! wait_for_http go_core "http://127.0.0.1:$go_core_host_port/health" 45 2; then
        go_core_ready=0
        echo "warning: go_core did not become healthy during bootstrap; continuing with observability stack" >&2
    fi
    run_prometheus
    wait_for_http prometheus "http://127.0.0.1:9090/-/ready" 60 2
    run_grafana
    wait_for_http grafana "http://127.0.0.1:3000/api/health" 60 2
    printf 'loki endpoint: http://127.0.0.1:3100\n'
    printf 'prometheus endpoint: http://127.0.0.1:9090\n'
    printf 'grafana endpoint: http://127.0.0.1:3000\n'
    printf 'go_core endpoint: http://127.0.0.1:%s\n' "$go_core_host_port"
    if [ "$go_core_ready" -ne 1 ]; then
        return 1
    fi
}

down() {
    remove_container_if_exists "$GRAFANA_CONTAINER"
    remove_container_if_exists "$PROMETHEUS_CONTAINER"
    remove_container_if_exists "$PROMTAIL_CONTAINER"
    remove_container_if_exists "$LOKI_CONTAINER"
    remove_container_if_exists "$GO_CORE_CONTAINER"
    remove_container_if_exists "$AI_KERNEL_CONTAINER"
    remove_container_if_exists "$RABBITMQ_CONTAINER"
    remove_container_if_exists "$POSTGRES_CONTAINER"
}

restart() {
    down
    up
}

bootstrap() {
    up
}

rebuild() {
    build_ai_kernel
    prepare_go_core_image
}

pin_go_core_image() {
    if [ "${2:-}" = "" ]; then
        echo "usage: $0 pin-go-core <image-ref>" >&2
        exit 1
    fi
    image_ref=$2
    if ! run_podman image exists "$image_ref"; then
        echo "go_core image not found: $image_ref" >&2
        exit 1
    fi
    write_go_core_target_image "$image_ref"
    printf 'go_core target image pinned: %s\n' "$image_ref"
}

print_go_core_target() {
    printf '%s\n' "$(read_go_core_target_image)"
}

status() {
    require_cmd curl
    require_cmd git

    desired_image=$(read_go_core_target_image)
    desired_version=$(read_go_core_target_version)
    go_core_host_port=$(resolve_go_core_host_port)
    git_head=$(git_full_commit)

    echo "== go_core target =="
    echo "target_image=$desired_image"
    echo "target_version=$desired_version"
    echo "git_head=$git_head"
    echo

    echo "== running container =="
    if container_exists "$GO_CORE_CONTAINER"; then
        run_podman inspect "$GO_CORE_CONTAINER" --format "image={{.ImageName}} created={{.Created}} status={{.State.Status}} running={{.State.Running}} host_port=$go_core_host_port"
        running_image=$(run_podman inspect "$GO_CORE_CONTAINER" --format '{{.ImageName}}')
        run_podman image inspect "$running_image" --format 'image_id={{.Id}} version={{index .Config.Labels "org.opencontainers.image.version"}} revision={{index .Config.Labels "org.opencontainers.image.revision"}} built={{index .Config.Labels "org.opencontainers.image.created"}}'
    else
        echo "go_core container is absent"
    fi
    echo

    echo "== health/full =="
    if ! curl -fsS "http://127.0.0.1:$go_core_host_port/health/full"; then
        echo "health/full unavailable"
    fi
    echo

    echo "== loki =="
    if ! curl -fsS "http://127.0.0.1:3100/ready"; then
        echo "loki unavailable"
    fi
    echo

    echo "== prometheus =="
    if ! curl -fsS "http://127.0.0.1:9090/-/ready"; then
        echo "prometheus unavailable"
    fi
    echo

    echo "== grafana =="
    if ! curl -fsS "http://127.0.0.1:3000/api/health"; then
        echo "grafana unavailable"
    fi
    echo

    echo "== resource policy =="
    echo "go_core_cpus=$GO_CORE_CPU_LIMIT"
    echo "go_core_memory=$GO_CORE_MEMORY_LIMIT"
    echo "go_core_memory_reservation=$GO_CORE_MEMORY_RESERVATION"
    echo "go_core_gomaxprocs=$GO_CORE_GOMAXPROCS"
    echo "go_core_gomemlimit=$GO_CORE_GOMEMLIMIT"
    echo "go_core_gogc=$GO_CORE_GOGC"
    echo "go_core_bootstrap_safe_mode=$GO_CORE_BOOTSTRAP_SAFE_MODE"
    echo "go_core_pg_skip_schema_ensure=$GO_CORE_PG_SKIP_SCHEMA_ENSURE"
    echo "go_core_pg_skip_vector_indexes=$GO_CORE_PG_SKIP_VECTOR_INDEXES"
    echo "go_core_pg_skip_vector_extension=$GO_CORE_PG_SKIP_VECTOR_EXTENSION"
    echo "go_core_pg_max_open_conns=$GO_CORE_PG_MAX_OPEN_CONNS"
    echo "go_core_pg_max_idle_conns=$GO_CORE_PG_MAX_IDLE_CONNS"
    echo "go_core_pg_schema_timeout=$GO_CORE_PG_SCHEMA_TIMEOUT"
    echo "go_core_log_buffer_size=$GO_CORE_LOG_BUFFER_SIZE"
    echo "ai_bridge_memory_enabled=$AI_BRIDGE_MEMORY_ENABLED"
    echo "go_core_cpu_reserve=$GO_CORE_CPU_RESERVE"
    echo "go_core_max_parallelism=$GO_CORE_MAX_PARALLELISM"
    echo "go_core_max_concurrent_tasks=$GO_CORE_MAX_CONCURRENT_TASKS"
    echo "go_core_max_concurrent_per_agent=$GO_CORE_MAX_CONCURRENT_PER_AGENT"
    echo "go_core_max_concurrent_per_model=$GO_CORE_MAX_CONCURRENT_PER_MODEL"
    echo "go_core_submit_workers=$GO_CORE_SUBMIT_WORKERS"
    echo "go_core_result_workers=$GO_CORE_RESULT_WORKERS"
    echo "go_core_agent_workers=$GO_CORE_AGENT_WORKERS"
    echo "go_core_provider_max_concurrent_per_key=$GO_CORE_PROVIDER_MAX_CONCURRENT_PER_KEY"
    echo "go_core_provider_max_concurrent_per_model=$GO_CORE_PROVIDER_MAX_CONCURRENT_PER_MODEL"
    echo "go_core_selflearn_enabled=$GO_CORE_SELFLEARN_ENABLED"
    echo "go_core_coding_runtime_enabled=$GO_CORE_CODING_RUNTIME_ENABLED"
    echo "go_core_provider_health_workers=$GO_CORE_PROVIDER_HEALTH_WORKERS"
    echo "go_core_provider_health_queue_size=$GO_CORE_PROVIDER_HEALTH_QUEUE_SIZE"
    echo
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
    go_core_host_port=$(resolve_go_core_host_port)
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
    echo "http://127.0.0.1:$go_core_host_port/health/full"
    curl -sS "http://127.0.0.1:$go_core_host_port/health/full"
    echo
    echo
    echo "== diagnostics logs =="
    echo "http://127.0.0.1:$go_core_host_port/diagnostics/logs?limit=20"
    curl -sS "http://127.0.0.1:$go_core_host_port/diagnostics/logs?limit=20"
    echo
    echo
    echo "== realtime metrics =="
    echo "http://127.0.0.1:$go_core_host_port/runtime/realtime_metrics"
    curl -sS "http://127.0.0.1:$go_core_host_port/runtime/realtime_metrics"
    echo
    echo
    echo "== loki ready =="
    curl -sS "http://127.0.0.1:3100/ready"
    echo
    echo
    echo "== prometheus ready =="
    curl -sS "http://127.0.0.1:9090/-/ready"
    echo
    echo
    echo "== prometheus up query =="
    curl -sS -G --data-urlencode 'query=up{job=~"go_core|loki|promtail|prometheus"}' "http://127.0.0.1:9090/api/v1/query"
    echo
    echo
    echo "== grafana health =="
    curl -sS "http://127.0.0.1:3000/api/health"
    echo
}

triage() {
    GO_CORE_HOST_PORT=$(resolve_go_core_host_port) "$ROOT_DIR/scripts/triage-observability.sh"
}

print_binary() {
    echo "$PODMAN_BIN"
}

usage() {
    cat <<'USAGE'
Usage:
  scripts/run-podman-stack.sh up
  scripts/run-podman-stack.sh bootstrap
  scripts/run-podman-stack.sh down
  scripts/run-podman-stack.sh restart
  scripts/run-podman-stack.sh rebuild
  scripts/run-podman-stack.sh pin-go-core <image-ref>
  scripts/run-podman-stack.sh print-go-core-target
  scripts/run-podman-stack.sh status
  scripts/run-podman-stack.sh diagnose
  scripts/run-podman-stack.sh triage
  scripts/run-podman-stack.sh ps
  scripts/run-podman-stack.sh logs <container>
  scripts/run-podman-stack.sh print-binary

Versioning:
  By default, go_core is built as localhost/go-core:<utc timestamp>-<git sha>
  and recorded in .runtime/go-core-image.ref.

  Set GO_CORE_IMAGE=<image-ref> to deploy an existing image without rebuilding.
  Set GO_CORE_VERSION=<version-id> to force a specific version tag during build.
  Set GO_CORE_HOST_PORT=<port> to pin the published go_core host port.
  Set GO_CORE_HOST_PORT=auto to pick the first free host port starting from 8010.

Resource policy:
  Set GO_CORE_CPU_LIMIT, GO_CORE_MEMORY_LIMIT, GO_CORE_MEMORY_RESERVATION, GO_CORE_PIDS_LIMIT
  to cap the orchestrator container.

  Set GO_CORE_GOMAXPROCS, GO_CORE_GOMEMLIMIT, GO_CORE_GOGC, GO_CORE_CPU_RESERVE,
  GO_CORE_MAX_PARALLELISM, GO_CORE_MAX_CONCURRENT_TASKS, GO_CORE_MAX_CONCURRENT_PER_AGENT,
  GO_CORE_MAX_CONCURRENT_PER_MODEL, GO_CORE_SUBMIT_WORKERS, GO_CORE_RESULT_WORKERS,
  GO_CORE_AGENT_WORKERS, GO_CORE_PROVIDER_MAX_CONCURRENT_PER_KEY,
  GO_CORE_PROVIDER_MAX_CONCURRENT_PER_MODEL, GO_CORE_SELFLEARN_ENABLED,
  GO_CORE_CODING_RUNTIME_ENABLED,
  GO_CORE_PROVIDER_HEALTH_WORKERS, and GO_CORE_PROVIDER_HEALTH_QUEUE_SIZE
  to keep warmup and background probing bounded.

Containers:
  postgresql -> ai_bridge_db
  rabbitmq   -> ai_bridge_rabbitmq
  ai_kernel  -> ai_bridge_ai_kernel
  loki       -> ai_bridge_loki
  promtail   -> ai_bridge_promtail
  prometheus -> ai_bridge_prometheus
  grafana    -> ai_bridge_grafana
  go_core    -> go_core
USAGE
}

choose_podman

case "${1:-}" in
    up)
        up
        ;;
    bootstrap)
        bootstrap
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
    pin-go-core)
        pin_go_core_image "$@"
        ;;
    print-go-core-target)
        print_go_core_target
        ;;
    status)
        status
        ;;
    diagnose)
        diagnose
        ;;
    triage)
        triage
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
