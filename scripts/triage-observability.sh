#!/bin/sh

set -eu

GO_CORE_PORT="${GO_CORE_HOST_PORT:-8010}"
BASE_URL="http://127.0.0.1:${GO_CORE_PORT}"

fetch() {
    label=$1
    url=$2
    echo "== ${label} =="
    echo "$url"
    if ! curl -fsS "$url"; then
        echo "unavailable"
    fi
    echo
    echo
}

query_prometheus() {
    label=$1
    query=$2
    echo "== prometheus: ${label} =="
    if ! curl -fsS -G --data-urlencode "query=${query}" "http://127.0.0.1:9090/api/v1/query"; then
        echo "query failed"
    fi
    echo
    echo
}

query_loki() {
    label=$1
    query=$2
    limit=${3:-20}
    echo "== loki: ${label} =="
    if ! curl -fsS -G --data-urlencode "query=${query}" --data-urlencode "limit=${limit}" "http://127.0.0.1:3100/loki/api/v1/query"; then
        echo "query failed"
    fi
    echo
    echo
}

fetch "go_core health" "$BASE_URL/health/full"
fetch "go_core diagnostics logs" "$BASE_URL/diagnostics/logs?limit=50"
fetch "go_core realtime metrics" "$BASE_URL/runtime/realtime_metrics"
fetch "loki ready" "http://127.0.0.1:3100/ready"
fetch "prometheus ready" "http://127.0.0.1:9090/-/ready"
fetch "grafana health" "http://127.0.0.1:3000/api/health"

query_prometheus "service up" 'up{job=~"go_core|loki|promtail|prometheus"}'
query_prometheus "realtime provider models" 'go_core_realtime_provider_models_total'
query_prometheus "realtime workflows" 'go_core_realtime_workflows_total'
query_prometheus "live sessions started" 'go_core_realtime_live_sessions_started_total'

query_loki "go_core warn and error logs" '{service="go_core"} | json | level=~"error|warn"' 30
query_loki "go_core http 5xx logs" '{service="go_core"} | json | status >= 500' 30
