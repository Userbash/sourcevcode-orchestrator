#!/usr/bin/env bash
set -Eeuo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
COMPOSE_FILE="${COMPOSE_FILE:-${ROOT_DIR}/docker-compose.ai.yml}"
DB_SERVICE="${DB_SERVICE:-db}"
PSQL_TIMEOUT="${PSQL_TIMEOUT:-15}"
SQL_EXTENSION="SELECT extname FROM pg_extension WHERE extname = 'vector';"
SQL_VECTOR_CAST="SELECT '[1,2,3]'::vector;"
COMPOSE_CMD=()

load_env() {
  local env_file="${ROOT_DIR}/.env.bridge"
  if [[ -f "$env_file" ]]; then
    set -a
    # shellcheck disable=SC1090
    . "$env_file"
    set +a
  fi
}

choose_compose() {
  if command -v podman >/dev/null 2>&1 && podman compose version >/dev/null 2>&1; then
    COMPOSE_CMD=(podman compose)
    return 0
  fi
  if command -v podman-compose >/dev/null 2>&1; then
    COMPOSE_CMD=(podman-compose)
    return 0
  fi
  if command -v docker >/dev/null 2>&1 && docker compose version >/dev/null 2>&1; then
    COMPOSE_CMD=(docker compose)
    return 0
  fi
  if command -v docker-compose >/dev/null 2>&1; then
    COMPOSE_CMD=(docker-compose)
    return 0
  fi
  return 1
}

compose() {
  "${COMPOSE_CMD[@]}" -f "$COMPOSE_FILE" "$@"
}

run_compose_sql() {
  local sql="$1"
  compose exec -T "$DB_SERVICE" sh -lc "psql -v ON_ERROR_STOP=1 -U \"\$POSTGRES_USER\" -d \"\$POSTGRES_DB\" -Atqc \"$sql\""
}

check_via_compose() {
  local cid
  cid="$(compose ps -q "$DB_SERVICE")"
  if [[ -z "$cid" ]]; then
    echo "ERROR: compose service '$DB_SERVICE' is not running" >&2
    return 1
  fi

  local extension_result
  extension_result="$(run_compose_sql "$SQL_EXTENSION")"
  if [[ "$extension_result" != "vector" ]]; then
    echo "ERROR: pgvector extension is not enabled in compose database" >&2
    return 1
  fi

  run_compose_sql "$SQL_VECTOR_CAST" >/dev/null
  echo "OK: pgvector verified through compose service '$DB_SERVICE'"
}

check_via_psql() {
  if ! command -v psql >/dev/null 2>&1; then
    echo "ERROR: psql is not installed and no compose runtime is available" >&2
    return 1
  fi
  if [[ -z "${AI_BRIDGE_MEMORY_DATABASE_URL:-}" ]]; then
    echo "ERROR: AI_BRIDGE_MEMORY_DATABASE_URL is not set and no compose runtime is available" >&2
    return 1
  fi

  local psql_args=("$AI_BRIDGE_MEMORY_DATABASE_URL" -v ON_ERROR_STOP=1 -Atqc)
  local extension_result
  extension_result="$(timeout "$PSQL_TIMEOUT" psql "${psql_args[@]}" "$SQL_EXTENSION")"
  if [[ "$extension_result" != "vector" ]]; then
    echo "ERROR: pgvector extension is not enabled in database URL target" >&2
    return 1
  fi

  timeout "$PSQL_TIMEOUT" psql "${psql_args[@]}" "$SQL_VECTOR_CAST" >/dev/null
  echo "OK: pgvector verified through AI_BRIDGE_MEMORY_DATABASE_URL"
}

main() {
  load_env

  if choose_compose; then
    check_via_compose
    exit 0
  fi

  check_via_psql
}

main "$@"
