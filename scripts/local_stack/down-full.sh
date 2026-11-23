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
KESSEL_COMPOSE_PROJECT="${COMPOSE_PROJECT_NAME:-full-kessel}"
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
    hbi_compose_file="${FULL_HBI_COMPOSE_FILE:-${HBI_REPO}/dev.yml}"
    hbi_compose_args=(
      -p "${HBI_COMPOSE_PROJECT}"
      --project-directory "${HBI_REPO}"
      -f "${hbi_compose_file}"
      -f "${REPO_ROOT}/scripts/local_stack/hbi.integration.yml"
    )
    if [[ -n "${FULL_STACK_SLOT:-}" ]]; then
      hbi_compose_args+=(-f "${REPO_ROOT}/scripts/local_stack/hbi.slot.override.yml")
    fi
    down_compose_with_orphans "${hbi_compose_args[@]}"
  ) || log-warn "HBI dev.yml compose down failed (may not be running)"
  remove_project_containers "${HBI_COMPOSE_PROJECT}"
else
  log-warn "HBI compose files not found; skipping HBI teardown"
fi

if [[ -n "${FULL_KESSEL_COMPOSE_DIR:-}" ]]; then
  compose_dir="${FULL_KESSEL_COMPOSE_DIR}"
  if [[ -f "${compose_dir}/docker-compose.yaml" ]]; then
    log-info "Stopping Kessel + Debezium + RBAC (${KESSEL_COMPOSE_PROJECT})..."
    "${COMPOSE_CMD[@]}" -p "${KESSEL_COMPOSE_PROJECT}" \
      --env-file "${compose_dir}/.env" \
      --profile consumer --profile rbac \
      -f "${compose_dir}/docker-compose.yaml" \
      -f "${REPO_ROOT}/scripts/local_stack/full-kessel.rbac-override.yml" \
      down --volumes --remove-orphans
  else
    log-warn "Slot Compose files not found; skipping Kessel stack teardown"
  fi
elif [[ -f "${INVENTORY_API_REPO}/scripts/stop-full-kessel.sh" ]]; then
  log-info "Stopping Kessel + Debezium + RBAC..."
  (
    cd "${INVENTORY_API_REPO}"
    export DOCKER="${CONTAINER_RUNTIME}"
    ./scripts/stop-full-kessel.sh
  )
else
  log-warn "inventory-api repo not found; skipping Kessel stack teardown"
fi

log-info "Full local stack stopped."
