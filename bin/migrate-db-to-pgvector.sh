#!/usr/bin/env bash
set -Eeuo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
COMPOSE_FILE="${COMPOSE_FILE:-${ROOT_DIR}/docker-compose.ai.yml}"
DB_SERVICE="${DB_SERVICE:-db}"
ORCHESTRATOR_SERVICE="${ORCHESTRATOR_SERVICE:-orchestrator}"
TARGET_IMAGE="${TARGET_IMAGE:-pgvector/pgvector:pg16}"
TARGET_MAJOR="${TARGET_MAJOR:-16}"
BACKUP_DIR="${BACKUP_DIR:-${ROOT_DIR}/db_migration_backups}"
TIMESTAMP="$(date -u +%Y%m%dT%H%M%SZ)"
BACKUP_FILE="${BACKUP_DIR}/pg_dumpall_${TIMESTAMP}.sql"

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
  echo "ERROR: no supported compose command found (tried podman compose, podman-compose, docker compose, docker-compose)" >&2
  exit 1
}

compose() {
  "${COMPOSE_CMD[@]}" -f "${COMPOSE_FILE}" "$@"
}

service_container_id() {
  compose ps -q "$1"
}

service_running() {
  local cid
  cid="$(service_container_id "$1")"
  [[ -n "$cid" ]]
}

inspect_image() {
  local cid="$1"
  if command -v podman >/dev/null 2>&1; then
    podman inspect --format '{{.Config.Image}}' "$cid"
    return 0
  fi
  if command -v docker >/dev/null 2>&1; then
    docker inspect --format '{{.Config.Image}}' "$cid"
    return 0
  fi
  echo "unknown"
}

wait_for_db() {
  local attempt=0
  until compose exec -T "$DB_SERVICE" sh -lc 'pg_isready -U "$POSTGRES_USER" -d "$POSTGRES_DB" >/dev/null 2>&1'; do
    attempt=$((attempt + 1))
    if [[ "$attempt" -ge 60 ]]; then
      echo "ERROR: database did not become ready after migration" >&2
      exit 1
    fi
    sleep 2
  done
}

current_db_major() {
  local version_num
  version_num="$(compose exec -T "$DB_SERVICE" sh -lc 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Atqc "SHOW server_version_num"')"
  if [[ -z "$version_num" ]]; then
    echo "ERROR: could not read server_version_num from PostgreSQL" >&2
    exit 1
  fi
  echo $((version_num / 10000))
}

backup_db() {
  mkdir -p "$BACKUP_DIR"
  echo "Creating logical backup at $BACKUP_FILE"
  compose exec -T "$DB_SERVICE" sh -lc 'PGPASSWORD="$POSTGRES_PASSWORD" pg_dumpall -U "$POSTGRES_USER"' > "$BACKUP_FILE"
}

ensure_vector_extension() {
  compose exec -T "$DB_SERVICE" sh -lc 'psql -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "$POSTGRES_DB" -c "CREATE EXTENSION IF NOT EXISTS vector;"'
}

main() {
  choose_compose

  if [[ ! -f "$COMPOSE_FILE" ]]; then
    echo "ERROR: compose file not found: $COMPOSE_FILE" >&2
    exit 1
  fi

  if ! service_running "$DB_SERVICE"; then
    echo "ERROR: service '$DB_SERVICE' is not running. Start the stack first so the script can inspect and back up the current database." >&2
    exit 1
  fi

  local db_cid
  db_cid="$(service_container_id "$DB_SERVICE")"
  local current_image
  current_image="$(inspect_image "$db_cid")"
  local current_major
  current_major="$(current_db_major)"

  echo "Current DB image: $current_image"
  echo "Current DB major version: $current_major"
  echo "Target DB image: $TARGET_IMAGE"
  echo "Target DB major version: $TARGET_MAJOR"

  backup_db

  if [[ "$current_major" != "$TARGET_MAJOR" ]]; then
    echo "ERROR: refusing in-place data-dir reuse across major versions ($current_major -> $TARGET_MAJOR)." >&2
    echo "Backup created: $BACKUP_FILE" >&2
    echo "Use a fresh pgvector volume and restore from the logical backup instead of reusing the old data directory." >&2
    exit 1
  fi

  if [[ "$current_image" == "$TARGET_IMAGE" ]]; then
    echo "Database already uses $TARGET_IMAGE. Ensuring vector extension exists."
    wait_for_db
    ensure_vector_extension
    echo "OK: pgvector image already active; extension ensured. Backup: $BACKUP_FILE"
    exit 0
  fi

  local orchestrator_was_running=0
  if service_running "$ORCHESTRATOR_SERVICE"; then
    orchestrator_was_running=1
    echo "Stopping $ORCHESTRATOR_SERVICE to avoid connection churn during DB recreation"
    compose stop "$ORCHESTRATOR_SERVICE"
  fi

  echo "Recreating $DB_SERVICE with compose image from $COMPOSE_FILE"
  compose rm -sf "$DB_SERVICE"
  compose up -d "$DB_SERVICE"

  wait_for_db
  ensure_vector_extension

  if [[ "$orchestrator_was_running" -eq 1 ]]; then
    echo "Starting $ORCHESTRATOR_SERVICE again"
    compose up -d "$ORCHESTRATOR_SERVICE"
  fi

  echo "Migration complete"
  echo "Logical backup: $BACKUP_FILE"
  echo "Next check: compose exec -T $DB_SERVICE sh -lc 'psql -U \"\$POSTGRES_USER\" -d \"\$POSTGRES_DB\" -Atqc \"SELECT extname FROM pg_extension WHERE extname = ''vector''\"'"
}

main "$@"
