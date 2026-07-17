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

POSTGRES_VOLUME="${POSTGRES_VOLUME:-hebrew_pg_data}"
RABBITMQ_VOLUME="${RABBITMQ_VOLUME:-f4e8a2ce6ed671173eddf888afcefc9489463ff48774ed94a56938d48b86a215}"
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
    if port=$(read_go_core_host_port 2>/dev/null); then
        printf '%s\n' "$port"
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
                -e AI_KERNEL_REQUIRE_API_KEY=false \
                -v "$AI_KERNEL_VOLUME":/models \
                "$AI_KERNEL_IMAGE"
            ;;
    esac
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
    remove_container_if_exists "$GO_CORE_CONTAINER"
    while :; do
        set +e
        run_output=$(
            run_podman run -d \
                --name "$GO_CORE_CONTAINER" \
                --network "$NETWORK_NAME" \
                --network-alias go_core \
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
                -e AI_KERNEL_REQUIRE_API_KEY=false \
                -v "$GO_CORE_VOLUME":/app/db_backups \
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
    prepare_go_core_image
    if [ "$AI_KERNEL_MODE" = "proxy-host" ]; then
        check_ai_kernel_upstream
    fi
    run_postgresql
    run_rabbitmq
    run_ai_kernel
    run_go_core
    go_core_host_port=$(resolve_go_core_host_port)
    wait_for_http go_core "http://127.0.0.1:$go_core_host_port/health/full" 90 2
    printf 'go_core endpoint: http://127.0.0.1:%s\n' "$go_core_host_port"
}

down() {
    remove_container_if_exists "$GO_CORE_CONTAINER"
    remove_container_if_exists "$AI_KERNEL_CONTAINER"
    remove_container_if_exists "$RABBITMQ_CONTAINER"
    remove_container_if_exists "$POSTGRES_CONTAINER"
}

restart() {
    down
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
}

print_binary() {
    echo "$PODMAN_BIN"
}

usage() {
    cat <<'USAGE'
Usage:
  scripts/run-podman-stack.sh up
  scripts/run-podman-stack.sh down
  scripts/run-podman-stack.sh restart
  scripts/run-podman-stack.sh rebuild
  scripts/run-podman-stack.sh pin-go-core <image-ref>
  scripts/run-podman-stack.sh print-go-core-target
  scripts/run-podman-stack.sh status
  scripts/run-podman-stack.sh diagnose
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

Containers:
  postgresql -> ai_bridge_db
  rabbitmq   -> ai_bridge_rabbitmq
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
