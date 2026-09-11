#!/usr/bin/env bash
# Start the full local integration stack:
#   Kessel (Inventory API + Relations + SpiceDB) + Debezium + RBAC + Host Inventory
#
# Uses project-kessel/inventory-api development/full-kessel (make kessel-up) for
# Kessel/Debezium/RBAC, then attaches insights-host-inventory on the `kessel`
# Docker network.
#
# Prerequisites:
#   docker or podman (with compose), curl
#   Optional sibling repos (auto-cloned into .local-deps/ if missing):
#     ../inventory-api  or  INVENTORY_API_REPO
#     ../insights-host-inventory  or  HBI_REPO
#
# Usage:
#   make docker-local-full-up local
#   make docker-local-full-up pr=https://github.com/project-kessel/insights-rbac/pull/3309
#   ./scripts/local_stack/up-full.sh
#   ./scripts/local_stack/up-full.sh --no-hbi
#   ./scripts/local_stack/up-full.sh --no-build
#   RBAC_IMAGE=my-rbac:dev ./scripts/local_stack/up-full.sh
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"
# shellcheck source=../common/logging.sh
source "${SCRIPT_DIR}/../common/logging.sh"
# shellcheck source=../common/container_runtime.sh
source "${SCRIPT_DIR}/../common/container_runtime.sh"

INVENTORY_API_REPO="${INVENTORY_API_REPO:-${KESSEL_REPO:-}}"
HBI_REPO="${HBI_REPO:-}"
RBAC_PR_NUMBER="${RBAC_PR_NUMBER:-3309}"
RBAC_IMAGE="${RBAC_IMAGE:-}"
RBAC_PR_URL="${RBAC_PR_URL:-}"
COMPOSE_PULL_MODE="${COMPOSE_PULL_MODE:-missing}"
SKIP_HBI=false
SKIP_BUILD=false
HBI_COMPOSE_PROJECT="${HBI_COMPOSE_PROJECT:-hbi-kessel-local}"
DEPLOYMENT_SOURCE="${RBAC_DEPLOYMENT_SOURCE:-local}"

usage() {
  cat <<'EOF'
Usage: up-full.sh [pr|local] [options]

  pr            Build the current checkout as insights-rbac-pr-<number>:dev.
  local         Build the current checkout as insights-rbac-local:dev (default).

  --no-hbi      Start Kessel + Debezium + RBAC only (skip Host Inventory)
  --no-build    Skip building the local RBAC image (use existing RBAC_IMAGE tag)
  -h, --help    Show this help

Environment:
  INVENTORY_API_REPO   Path to project-kessel/inventory-api checkout
  HBI_REPO             Path to RedHatInsights/insights-host-inventory checkout
  RBAC_IMAGE           Docker image tag for RBAC services (default depends on source)
  RBAC_PR_NUMBER       PR number used by the pr source (default: 3309)
  RBAC_PR_URL           GitHub PR URL; fetched into a temporary worktree in pr mode
  COMPOSE_PULL_MODE    Passed to inventory-api start-full-kessel (default: missing)
  INVENTORY_DB_PORT    Host port for HBI Postgres (default: 15433)
  HBI_WEB_PORT         Host port for HBI API (default: 8080)
  UNLEASH_TOKEN        Required by Host Inventory dev.yml parsing (default: local-dev-token)
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    pr|local) DEPLOYMENT_SOURCE="$1"; shift ;;
    --no-hbi) SKIP_HBI=true; shift ;;
    --no-build) SKIP_BUILD=true; shift ;;
    -h | --help) usage; exit 0 ;;
    *)
      log-err "Unknown option: $1"
      usage
      exit 1
      ;;
  esac
done

case "${DEPLOYMENT_SOURCE}" in
  pr)
    RBAC_IMAGE="${RBAC_IMAGE:-insights-rbac-pr-${RBAC_PR_NUMBER}:dev}"
    ;;
  local)
    RBAC_IMAGE="${RBAC_IMAGE:-insights-rbac-local:dev}"
    ;;
  *)
    log-err "Unknown deployment source '${DEPLOYMENT_SOURCE}'. Expected 'pr' or 'local'."
    usage
    exit 1
    ;;
esac

require_cmd() {
  if ! command -v "$1" &>/dev/null; then
    log-err "Required command not found: $1"
    exit 1
  fi
}

start_pr_worktree() {
  [[ "${DEPLOYMENT_SOURCE}" == pr && -n "${RBAC_PR_URL}" ]] || return 0
  [[ -z "${RBAC_PR_WORKTREE:-}" ]] || return 0

  local repository pr_number pr_worktree status
  if [[ "${RBAC_PR_URL}" =~ ^https://github\.com/([^/]+/[^/]+)/pull/([0-9]+)(/.*)?$ ]]; then
    repository="https://github.com/${BASH_REMATCH[1]}.git"
    pr_number="${BASH_REMATCH[2]}"
  else
    log-err "RBAC_PR_URL must be a GitHub pull request URL: ${RBAC_PR_URL}"
    exit 1
  fi

  pr_worktree="$(mktemp -d "${TMPDIR:-/tmp}/insights-rbac-pr-${pr_number}.XXXXXX")"
  rmdir "${pr_worktree}"
  log-info "Fetching PR #${pr_number} from ${repository}..."
  git -C "${REPO_ROOT}" fetch --no-tags "${repository}" "pull/${pr_number}/head"
  git -C "${REPO_ROOT}" worktree add --detach "${pr_worktree}" FETCH_HEAD >/dev/null

  cp "${SCRIPT_DIR}/up-full.sh" "${pr_worktree}/scripts/local_stack/up-full.sh"
  cp "${SCRIPT_DIR}/full-kessel.rbac-override.yml" \
    "${pr_worktree}/scripts/local_stack/full-kessel.rbac-override.yml"
  chmod +x "${pr_worktree}/scripts/local_stack/up-full.sh"

  log-info "Using PR #${pr_number} checkout at ${pr_worktree}"
  local child_args=(pr)
  [[ "${SKIP_HBI}" == true ]] && child_args+=(--no-hbi)
  [[ "${SKIP_BUILD}" == true ]] && child_args+=(--no-build)

  if RBAC_PR_WORKTREE=true RBAC_PR_URL= RBAC_PR_NUMBER="${pr_number}" \
    RBAC_IMAGE="insights-rbac-pr-${pr_number}:dev" \
    "${pr_worktree}/scripts/local_stack/up-full.sh" "${child_args[@]}"; then
    status=0
  else
    status=$?
  fi

  git -C "${REPO_ROOT}" worktree remove --force "${pr_worktree}" >/dev/null 2>&1 || true
  exit "${status}"
}

resolve_inventory_api_repo() {
  if [[ -z "${INVENTORY_API_REPO}" || ! -f "${INVENTORY_API_REPO}/scripts/start-full-kessel.sh" ]]; then
    INVENTORY_API_REPO="$(dirname "${REPO_ROOT}")/inventory-api"
  fi
  if [[ ! -f "${INVENTORY_API_REPO}/scripts/start-full-kessel.sh" ]]; then
    local clone_dir="${REPO_ROOT}/.local-deps/inventory-api"
    if [[ ! -f "${clone_dir}/scripts/start-full-kessel.sh" ]]; then
      log-info "Cloning inventory-api into ${clone_dir}..."
      mkdir -p "${REPO_ROOT}/.local-deps"
      git clone --depth 1 https://github.com/project-kessel/inventory-api.git "${clone_dir}"
    fi
    INVENTORY_API_REPO="${clone_dir}"
  fi
  log-info "Using inventory-api at ${INVENTORY_API_REPO}"
}

resolve_hbi_repo() {
  if [[ -z "${HBI_REPO}" || ! -f "${HBI_REPO}/dev.yml" ]]; then
    HBI_REPO="$(dirname "${REPO_ROOT}")/insights-host-inventory"
  fi
  if [[ ! -f "${HBI_REPO}/dev.yml" ]]; then
    local clone_dir="${REPO_ROOT}/.local-deps/insights-host-inventory"
    if [[ ! -f "${clone_dir}/dev.yml" ]]; then
      log-info "Cloning insights-host-inventory into ${clone_dir}..."
      mkdir -p "${REPO_ROOT}/.local-deps"
      git clone --depth 1 https://github.com/RedHatInsights/insights-host-inventory.git "${clone_dir}"
    fi
    HBI_REPO="${clone_dir}"
  fi
  log-info "Using Host Inventory at ${HBI_REPO}"
  log-info "Initializing Host Inventory git submodules..."
  git -C "${HBI_REPO}" submodule update --init --recursive
}

start_kessel_stack() {
  export RBAC_IMAGE
  export COMPOSE_PULL_MODE
  export DOCKER="${CONTAINER_RUNTIME}"
  log-info "Starting Kessel + Debezium + RBAC (RBAC_IMAGE=${RBAC_IMAGE})..."
  "${SCRIPT_DIR}/start-kessel-compose.sh" \
    "${INVENTORY_API_REPO}" \
    "${REPO_ROOT}/scripts/local_stack/full-kessel.rbac-override.yml"
}

start_hbi() {
  export UNLEASH_TOKEN="${UNLEASH_TOKEN:-local-dev-token}"
  export INVENTORY_DB_PORT="${INVENTORY_DB_PORT:-15433}"
  export HBI_WEB_PORT="${HBI_WEB_PORT:-8080}"

  log-info "Creating HBI Kafka topics on Kessel broker..."
  "${SCRIPT_DIR}/ensure-hbi-kafka-topics.sh" "${INVENTORY_API_REPO}"

  log-info "Building and starting Host Inventory from ${HBI_REPO}..."

  "${COMPOSE_CMD[@]}" -p "${HBI_COMPOSE_PROJECT}" \
    -f "${HBI_REPO}/dev.yml" \
    -f "${REPO_ROOT}/scripts/local_stack/hbi.integration.yml" \
    up -d --build --no-deps db hbi-web hbi-mq
}

print_endpoints() {
  cat <<EOF

Stack endpoints:
  RBAC API:          http://localhost:9080
  RBAC Postgres:     localhost:15432
  Relations API:     localhost:9000
  SpiceDB (zed):     localhost:50051
  Inventory API:     localhost:9081
  Kafka Connect:     http://localhost:8083
  HBI API:           http://localhost:${HBI_WEB_PORT:-8080}
  HBI Postgres:      localhost:${INVENTORY_DB_PORT:-15433}

Verify workspace create + RYW (after stack is healthy):
  ./scripts/create_workspace_local.sh --no-start

Verify SpiceDB tuples:
  ./scripts/zed_local.sh check

EOF
}

require_cmd curl
require_cmd git

start_pr_worktree

detect_container_runtime

resolve_inventory_api_repo

if [[ "${SKIP_BUILD}" != true ]]; then
  log-info "Building local RBAC image ${RBAC_IMAGE}..."
  "${CONTAINER_RUNTIME}" build -t "${RBAC_IMAGE}" "${REPO_ROOT}"
  export RBAC_FORCE_RECREATE=true
else
  log-info "Skipping RBAC image build (RBAC_IMAGE=${RBAC_IMAGE})"
  export RBAC_FORCE_RECREATE=false
fi

start_kessel_stack

if [[ "${SKIP_HBI}" != true ]]; then
  resolve_hbi_repo
  start_hbi
else
  log-info "Skipping Host Inventory (--no-hbi)"
fi

log-info "Full local stack started."
print_endpoints
