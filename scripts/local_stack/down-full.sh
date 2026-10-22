#!/usr/bin/env bash
# Tear down the full local integration stack (HBI + Kessel/Debezium/RBAC).
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"
# shellcheck source=../common/logging.sh
source "${SCRIPT_DIR}/../common/logging.sh"
# shellcheck source=../common/container_runtime.sh
source "${SCRIPT_DIR}/../common/container_runtime.sh"

detect_container_runtime

INVENTORY_API_REPO="${INVENTORY_API_REPO:-${REPO_ROOT}/.local-deps/inventory-api}"
HBI_REPO="${HBI_REPO:-${REPO_ROOT}/.local-deps/insights-host-inventory}"
HBI_COMPOSE_PROJECT="${HBI_COMPOSE_PROJECT:-hbi-kessel-local}"
LEGACY_COMPOSE_PROJECT="${LEGACY_COMPOSE_PROJECT:-insights-rbac}"
REMOVE_VOLUMES=false

usage() {
  cat <<'EOF'
Usage: down-full.sh [options]

  -v, --volumes   Remove compose volumes when stopping HBI
  -h, --help      Show this help
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    -v | --volumes) REMOVE_VOLUMES=true; shift ;;
    -h | --help) usage; exit 0 ;;
    *)
      log-err "Unknown option: $1"
      usage
      exit 1
      ;;
  esac
done

down_compose() {
  if [[ "${REMOVE_VOLUMES}" == true ]]; then
    "${COMPOSE_CMD[@]}" "$@" down -v
  else
    "${COMPOSE_CMD[@]}" "$@" down
  fi
}

down_compose_with_orphans() {
  if [[ "${REMOVE_VOLUMES}" == true ]]; then
    "${COMPOSE_CMD[@]}" "$@" down -v --remove-orphans
  else
    "${COMPOSE_CMD[@]}" "$@" down --remove-orphans
  fi
}

remove_project_containers() {
  local project="$1"
  local container label

  for label in com.docker.compose.project io.podman.compose.project; do
    while IFS= read -r container; do
      [[ -n "${container}" ]] || continue
      log-info "Removing leftover ${project} container: ${container}"
      "${CONTAINER_RUNTIME}" rm -f "${container}" >/dev/null || \
        log-warn "Could not remove ${project} container: ${container}"
    done < <("${CONTAINER_RUNTIME}" ps -a \
      --filter "label=${label}=${project}" \
      --format '{{.Names}}')
  done
}

if [[ -f "${HBI_REPO}/dev.yml" ]]; then
  log-info "Stopping Host Inventory (${HBI_COMPOSE_PROJECT})..."
  (
    # Compose validates this required variable even when only stopping services.
    export UNLEASH_TOKEN="${UNLEASH_TOKEN:-local-dev-token}"
    down_compose_with_orphans -p "${HBI_COMPOSE_PROJECT}" \
      -f "${HBI_REPO}/dev.yml" \
      -f "${REPO_ROOT}/scripts/local_stack/hbi.integration.yml"
  ) || log-warn "HBI dev.yml compose down failed (may not be running)"
  remove_project_containers "${HBI_COMPOSE_PROJECT}"
else
  log-warn "HBI compose files not found; skipping HBI teardown"
fi

if [[ -f "${INVENTORY_API_REPO}/scripts/stop-full-kessel.sh" ]]; then
  log-info "Stopping Kessel + Debezium + RBAC..."
  (
    cd "${INVENTORY_API_REPO}"
    export DOCKER="${CONTAINER_RUNTIME}"
    ./scripts/stop-full-kessel.sh
  )
else
  log-warn "inventory-api repo not found; skipping Kessel stack teardown"
fi

if [[ -f "${REPO_ROOT}/docker-compose.yml" ]]; then
  log-info "Stopping legacy RBAC Compose project (${LEGACY_COMPOSE_PROJECT})..."
  down_compose -p "${LEGACY_COMPOSE_PROJECT}" \
    -f "${REPO_ROOT}/docker-compose.yml" || log-warn "legacy docker-compose.yml down failed (may not be running)"
fi

if [[ -f "${REPO_ROOT}/docker-compose.local.yml" ]]; then
  log-info "Stopping legacy local RBAC Compose project (${LEGACY_COMPOSE_PROJECT})..."
  down_compose -p "${LEGACY_COMPOSE_PROJECT}" \
    -f "${REPO_ROOT}/docker-compose.local.yml" || log-warn "legacy docker-compose.local.yml down failed (may not be running)"
fi
remove_project_containers "${LEGACY_COMPOSE_PROJECT}"

log-info "Full local stack stopped."
