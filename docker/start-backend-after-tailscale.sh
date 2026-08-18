#!/usr/bin/env bash
set -euo pipefail

readonly PROJECT_DIR="/home/shixian/project/xbly"
readonly TAILSCALE_IP="100.83.127.12"
readonly BACKEND_PORT="18000"
readonly -a COMPOSE_ARGS=(
    -f "$PROJECT_DIR/compose.yaml"
    -f "$PROJECT_DIR/compose.backend.yaml"
    -f "$PROJECT_DIR/compose.blue-green.yaml"
)

log() {
    /usr/bin/logger --tag bbw-startup -- "$*"
}

container_health() {
    /usr/bin/docker inspect \
        --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}' \
        "$1" 2>/dev/null || true
}

log "waiting for Tailscale"
/usr/bin/tailscale wait

tailscale_ip_ready=false
for _attempt in $(/usr/bin/seq 1 150); do
    if /usr/bin/tailscale ip --assert="$TAILSCALE_IP" >/dev/null 2>&1; then
        tailscale_ip_ready=true
        break
    fi
    /usr/bin/sleep 2
done
if [[ "$tailscale_ip_ready" != true ]]; then
    log "expected Tailscale IP $TAILSCALE_IP was not assigned"
    exit 1
fi

docker_ready=false
for _attempt in $(/usr/bin/seq 1 150); do
    if /usr/bin/docker info >/dev/null 2>&1; then
        docker_ready=true
        break
    fi
    /usr/bin/sleep 2
done
if [[ "$docker_ready" != true ]]; then
    log "Docker did not become ready"
    exit 1
fi

dependencies_ready=false
for _attempt in $(/usr/bin/seq 1 90); do
    postgres_health="$(container_health bbw-postgres-1)"
    redis_health="$(container_health bbw-redis-1)"
    migrate_state="$(
        /usr/bin/docker inspect \
            --format '{{.State.Status}}:{{.State.ExitCode}}' \
            bbw-migrate-1 2>/dev/null || true
    )"
    if [[ "$postgres_health" == healthy \
        && "$redis_health" == healthy \
        && "$migrate_state" == exited:0 ]]; then
        dependencies_ready=true
        break
    fi
    /usr/bin/sleep 2
done
if [[ "$dependencies_ready" != true ]]; then
    log "backend dependencies did not become ready"
    exit 1
fi

cd "$PROJECT_DIR"
if ! /usr/bin/docker compose "${COMPOSE_ARGS[@]}" \
    up -d --no-deps --no-build app >/dev/null 2>&1; then
    log "failed to start the app container"
    exit 1
fi

app_healthy=false
for _attempt in $(/usr/bin/seq 1 75); do
    app_health="$(container_health bbw-app-1)"
    if [[ "$app_health" == healthy ]]; then
        app_healthy=true
        break
    fi
    if [[ "$app_health" == exited || -z "$app_health" ]]; then
        log "app container stopped before becoming healthy"
        exit 1
    fi
    /usr/bin/sleep 2
done
if [[ "$app_healthy" != true ]]; then
    log "app container did not become healthy"
    exit 1
fi

if ! /usr/bin/curl --noproxy '*' --fail --silent --show-error \
    --max-time 8 "http://$TAILSCALE_IP:$BACKEND_PORT/readyz" >/dev/null; then
    log "backend readiness endpoint failed"
    exit 1
fi

log "backend startup completed successfully"
