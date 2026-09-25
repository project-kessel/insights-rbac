#!/usr/bin/env bash
# Check that the full local Kessel + RBAC + HBI stack is running and reachable.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=../common/logging.sh
source "${SCRIPT_DIR}/../common/logging.sh"
# shellcheck source=../common/container_runtime.sh
source "${SCRIPT_DIR}/../common/container_runtime.sh"

detect_container_runtime

FAILURES=0

fail() {
  log-err "$*"
  FAILURES=$((FAILURES + 1))
}

container_names() {
  "${CONTAINER_RUNTIME}" ps -a --format '{{.Names}}' | grep -E "$1" || true
}

check_containers() {
  # Verify that all containers matching a name pattern are running and
  # healthy. Treats exited one-shot jobs (migrate, init, setup) with
  # exit code 0 as successfully completed.
  local pattern="$1"
  local label="$2"
  local names name state health exit_code

  names="$(container_names "${pattern}")"
  if [[ -z "${names}" ]]; then
    fail "${label}: no matching containers found"
    return
  fi

  while IFS= read -r name; do
    [[ -n "${name}" ]] || continue
    state=$("${CONTAINER_RUNTIME}" inspect --format '{{.State.Status}}' "${name}")
    health=$("${CONTAINER_RUNTIME}" inspect --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}' "${name}")
    exit_code=$("${CONTAINER_RUNTIME}" inspect --format '{{.State.ExitCode}}' "${name}")

    # Compose setup containers are one-shot jobs. A clean exit means the
    # initialization completed successfully and is not a failed runtime.
    if [[ "${state}" == exited && "${exit_code}" == 0 && "${name}" =~ (migrate|init|setup)-[0-9]+$ ]]; then
      log-info "${name}: completed successfully"
      continue
    fi
    if [[ "${state}" != running ]]; then
      fail "${name}: state=${state} exit_code=${exit_code}"
      continue
    fi
    if [[ "${health}" != none && "${health}" != healthy ]]; then
      fail "${name}: state=${state} health=${health}"
      continue
    fi
    log-info "${name}: state=${state} health=${health}"
  done <<< "${names}"
}

check_tcp() {
  local host="$1"
  local port="$2"
  local label="$3"
  if (echo >/dev/tcp/${host}/${port}) >/dev/null 2>&1; then
    log-info "${label}: ${host}:${port} reachable"
  else
    fail "${label}: ${host}:${port} is not reachable"
  fi
}

check_http() {
  local url="$1"
  local label="$2"
  if curl -fsS --max-time 5 "${url}" >/dev/null 2>&1; then
    log-info "${label}: ${url} reachable"
  else
    fail "${label}: ${url} is not reachable"
  fi
}

log-info "Checking full Kessel, RBAC, and HBI containers..."
check_containers '^full-kessel-' 'Kessel/RBAC containers'
check_containers '^hbi-kessel-local-' 'HBI containers'

log-info "Checking published full-stack endpoints..."
check_http "${RBAC_HEALTH_URL:-http://localhost:9080/metrics}" 'RBAC API'
check_http "${INVENTORY_HEALTH_URL:-http://localhost:8081/api/kessel/v1/livez}" 'Kessel Inventory API'
check_http "${KAFKA_CONNECT_HEALTH_URL:-http://localhost:8083/connectors}" 'Kafka Connect'
check_tcp localhost "${RBAC_DB_PORT:-15432}" 'RBAC PostgreSQL'
check_tcp localhost "${RELATIONS_PORT:-9000}" 'Kessel Relations API'
check_tcp localhost "${SPICEDB_PORT:-50051}" 'SpiceDB'

if [[ "${FAILURES}" -gt 0 ]]; then
  log-err "Full local stack is not healthy (${FAILURES} check(s) failed)."
  exit 1
fi

log-info 'Full local stack is healthy.'
