#!/bin/sh

set -eu

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
ROOT_DIR=$(CDPATH= cd -- "$SCRIPT_DIR/.." && pwd)
GO_CORE_DIR="$ROOT_DIR/go-core"
RUNTIME_DIR="${ORCHESTRATOR_RUNTIME_DIR:-$ROOT_DIR/.runtime/orchestrator-host}"
PID_FILE="$RUNTIME_DIR/orchestrator.pid"
LOG_FILE="$RUNTIME_DIR/orchestrator.log"
mkdir -p "$RUNTIME_DIR"

load_env_file() {
    file_path=$1
    if [ -f "$file_path" ]; then
        set -a
        # shellcheck disable=SC1090
        . "$file_path"
        set +a
    fi
}

load_env_file "$ROOT_DIR/.env"
load_env_file "$ROOT_DIR/.env.bridge"
load_env_file "$ROOT_DIR/.env.gemini.local"

COMPOSE_FILE="${COMPOSE_FILE:-docker-compose.ai.yml}"
ORCHESTRATOR_PORT="${ORCHESTRATOR_PORT:-8000}"
GO_CORE_ADDR="${GO_CORE_ADDR:-0.0.0.0:$ORCHESTRATOR_PORT}"
HOST_LOOPBACK="${ORCHESTRATOR_HOST_LOOPBACK:-127.0.0.1}"

export GO_CORE_ADDR
export AI_BRIDGE_LOCAL_LLM_ENDPOINT="${AI_BRIDGE_LOCAL_LLM_ENDPOINT:-http://127.0.0.1:${AI_BRIDGE_LOCAL_LLM_PORT:-11434}}"
export AI_KERNEL_BASE_URL="${AI_KERNEL_BASE_URL:-http://127.0.0.1:${AI_KERNEL_PORT:-8012}/v1}"
export AI_BRIDGE_MESSAGE_BUS_BACKEND="${AI_BRIDGE_MESSAGE_BUS_BACKEND:-rabbitmq}"
export GOMODCACHE="${GOMODCACHE:-$ROOT_DIR/.gomodcache}"
export GOCACHE="${GOCACHE:-$ROOT_DIR/.gocache}"

if [ -z "${AI_BRIDGE_MEMORY_DATABASE_URL:-}" ]; then
    export AI_BRIDGE_MEMORY_DATABASE_HOST="${AI_BRIDGE_MEMORY_DATABASE_HOST:-127.0.0.1}"
fi

if [ -z "${AI_BRIDGE_RABBITMQ_URL:-}" ]; then
    export AI_BRIDGE_RABBITMQ_HOST="${AI_BRIDGE_RABBITMQ_HOST:-127.0.0.1}"
fi

choose_runner() {
    if [ -n "${ORCHESTRATOR_BIN:-}" ] && [ -x "${ORCHESTRATOR_BIN}" ]; then
        printf '%s\n' "$ORCHESTRATOR_BIN"
        return
    fi

    if [ -x "$GO_CORE_DIR/orchestrator" ]; then
        printf '%s\n' "$GO_CORE_DIR/orchestrator"
        return
    fi

    printf '%s\n' "go run ./cmd/orchestrator"
}

RUNNER=$(choose_runner)

is_running() {
    if [ ! -f "$PID_FILE" ]; then
        return 1
    fi

    pid=$(cat "$PID_FILE")
    if [ -z "$pid" ]; then
        return 1
    fi

    if kill -0 "$pid" 2>/dev/null; then
        return 0
    fi

    rm -f "$PID_FILE"
    return 1
}

run_server() {
    cd "$GO_CORE_DIR"
    exec sh -c "$RUNNER serve --addr \"$GO_CORE_ADDR\" --ensure-ai-stack --project-root \"$ROOT_DIR\" --compose-file \"$COMPOSE_FILE\""
}

start_background() {
    if is_running; then
        echo "orchestrator already running: pid=$(cat "$PID_FILE")"
        return 0
    fi

    nohup "$0" start-foreground >"$LOG_FILE" 2>&1 &
    pid=$!
    echo "$pid" >"$PID_FILE"
    sleep 1

    if kill -0 "$pid" 2>/dev/null; then
        echo "orchestrator started: pid=$pid"
        echo "log=$LOG_FILE"
        return 0
    fi

    echo "orchestrator failed to start; inspect $LOG_FILE" >&2
    rm -f "$PID_FILE"
    return 1
}

stop_process() {
    if ! is_running; then
        echo "orchestrator is not running"
        return 0
    fi

    pid=$(cat "$PID_FILE")
    kill "$pid"

    i=0
    while kill -0 "$pid" 2>/dev/null; do
        i=$((i + 1))
        if [ "$i" -ge 20 ]; then
            echo "orchestrator did not stop gracefully; sending SIGKILL"
            kill -9 "$pid" 2>/dev/null || true
            break
        fi
        sleep 1
    done

    rm -f "$PID_FILE"
    echo "orchestrator stopped"
}

status_process() {
    if is_running; then
        pid=$(cat "$PID_FILE")
        echo "status=running pid=$pid addr=$GO_CORE_ADDR"
    else
        echo "status=stopped addr=$GO_CORE_ADDR"
    fi

    if command -v curl >/dev/null 2>&1; then
        health_url="http://$HOST_LOOPBACK:$ORCHESTRATOR_PORT/health"
        if curl -fsS "$health_url" >/dev/null 2>&1; then
            echo "health=ok url=$health_url"
        else
            echo "health=unreachable url=$health_url"
        fi
    fi
}

print_logs() {
    if [ ! -f "$LOG_FILE" ]; then
        echo "no log file: $LOG_FILE" >&2
        return 1
    fi

    tail -n "${TAIL_LINES:-200}" "$LOG_FILE"
}

print_ws_url() {
    echo "ws://$HOST_LOOPBACK:$ORCHESTRATOR_PORT/chat/ws"
}

usage() {
    cat <<USAGE
Usage: $(basename "$0") <command>

Commands:
  start             Start orchestrator in background
  start-foreground  Start orchestrator in foreground
  stop              Stop background orchestrator
  restart           Restart background orchestrator
  status            Show process and health status
  logs              Tail runtime log
  ws-url            Print chat websocket URL
USAGE
}

command_name=${1:-}

case "$command_name" in
    start)
        start_background
        ;;
    start-foreground)
        run_server
        ;;
    stop)
        stop_process
        ;;
    restart)
        stop_process || true
        start_background
        ;;
    status)
        status_process
        ;;
    logs)
        print_logs
        ;;
    ws-url)
        print_ws_url
        ;;
    *)
        usage >&2
        exit 1
        ;;
esac
