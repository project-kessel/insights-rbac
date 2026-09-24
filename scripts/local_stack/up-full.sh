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
#   Kessel Inventory and Host Inventory use the selected local, upstream, PR,
#   or commit source.
#
# Usage:
#   make docker-local-full-up rbac=local rbac-config=upstream
#   make docker-local-full-up inventory=<inventory-pr-url> hbi=<hbi-pr-url>
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"
# shellcheck source=../common/logging.sh
source "${SCRIPT_DIR}/../common/logging.sh"
# shellcheck source=../common/container_runtime.sh
source "${SCRIPT_DIR}/../common/container_runtime.sh"

RBAC_UPSTREAM_REPO_URL="https://github.com/project-kessel/insights-rbac.git"
RBAC_CONFIG_UPSTREAM_REPO_URL="https://github.com/project-kessel/rbac-config.git"
INVENTORY_API_UPSTREAM_REPO_URL="https://github.com/project-kessel/inventory-api.git"
HBI_UPSTREAM_REPO_URL="https://github.com/RedHatInsights/insights-host-inventory.git"

RBAC_LOCAL_REPO_WAS_SET="${RBAC_LOCAL_REPO+x}"
RBAC_LOCAL_REPO_FROM_ENV="${RBAC_LOCAL_REPO-}"
RBAC_CONFIG_REPO_WAS_SET="${RBAC_CONFIG_REPO+x}"
RBAC_CONFIG_REPO_FROM_ENV="${RBAC_CONFIG_REPO-}"
INVENTORY_API_REPO_WAS_SET="${INVENTORY_API_REPO+x}"
INVENTORY_API_REPO_FROM_ENV="${INVENTORY_API_REPO-}"
HBI_REPO_WAS_SET="${HBI_REPO+x}"
HBI_REPO_FROM_ENV="${HBI_REPO-}"
LOCAL_STACK_CONFIG_FILE="${XDG_CONFIG_HOME:-${HOME}/.config}/insights-rbac/local-stack.env"
INVENTORY_API_REPO="${INVENTORY_API_REPO:-}"
HBI_REPO="${HBI_REPO:-}"
RBAC_IMAGE="${RBAC_IMAGE:-}"
RBAC_SOURCE="${RBAC_SOURCE:-local}"
RBAC_CONFIG_SOURCE="${RBAC_CONFIG_SOURCE:-upstream}"
INVENTORY_SOURCE="${INVENTORY_SOURCE:-upstream}"
HBI_SOURCE="${HBI_SOURCE:-upstream}"
RBAC_LOCAL_REPO="${RBAC_LOCAL_REPO:-}"
RBAC_PR_NUMBER=""
RBAC_PR_URL=""
RBAC_CONFIG_PR_URL=""
RBAC_CONFIG_REPO="${RBAC_CONFIG_REPO:-}"
DEFAULT_RBAC_CONFIG_REPO="${REPO_ROOT}/../rbac-config"
RBAC_SOURCE_KIND="local"
RBAC_SOURCE_REF=""
RBAC_CONFIG_SOURCE_KIND="upstream"
RBAC_CONFIG_SOURCE_REF=""
INVENTORY_SOURCE_KIND="upstream"
INVENTORY_SOURCE_REF=""
INVENTORY_SOURCE_REPO="${INVENTORY_API_UPSTREAM_REPO_URL}"
INVENTORY_PR_NUMBER=""
HBI_SOURCE_KIND="upstream"
HBI_SOURCE_REF=""
HBI_SOURCE_REPO="${HBI_UPSTREAM_REPO_URL}"
HBI_PR_NUMBER=""
COMPOSE_PULL_MODE="${COMPOSE_PULL_MODE:-missing}"
HBI_COMPOSE_PROJECT="${HBI_COMPOSE_PROJECT:-hbi-kessel-local}"
DEFAULT_USERS_FIXTURE="${FULL_STACK_DEFAULT_USERS_FIXTURE:-${REPO_ROOT}/scripts/validations/api/actions/full-stack-default-users.yaml}"
DEFAULT_USERS_APPLY_SCRIPT="${FULL_STACK_DEFAULT_USERS_APPLY_SCRIPT:-${REPO_ROOT}/scripts/validations/api/actions/apply-rbac-users-config.sh}"
STACK_WAS_RUNNING=false
RBAC_SOURCE_LABEL="${RBAC_SOURCE_LABEL:-${RBAC_SOURCE}}"
RBAC_CONFIG_SOURCE_LABEL="${RBAC_CONFIG_SOURCE_LABEL:-${RBAC_CONFIG_SOURCE}}"
INVENTORY_SOURCE_LABEL="${INVENTORY_SOURCE_LABEL:-${INVENTORY_SOURCE}}"
HBI_SOURCE_LABEL="${HBI_SOURCE_LABEL:-${HBI_SOURCE}}"
unset RBAC_CONFIG_FILE SCHEMA_ZED_FILE RBAC_CONFIG_URL SCHEMA_ZED_URL

if [[ -f "${LOCAL_STACK_CONFIG_FILE}" ]]; then
  INVENTORY_API_REPO_WAS_IN_CONFIG=false
  HBI_REPO_WAS_IN_CONFIG=false
  if grep -q '^INVENTORY_API_REPO=' "${LOCAL_STACK_CONFIG_FILE}"; then
    INVENTORY_API_REPO_WAS_IN_CONFIG=true
  fi
  if grep -q '^HBI_REPO=' "${LOCAL_STACK_CONFIG_FILE}"; then
    HBI_REPO_WAS_IN_CONFIG=true
  fi
  # shellcheck disable=SC1090
  source "${LOCAL_STACK_CONFIG_FILE}"
else
  INVENTORY_API_REPO_WAS_IN_CONFIG=false
  HBI_REPO_WAS_IN_CONFIG=false
fi
if [[ "${RBAC_LOCAL_REPO_WAS_SET}" == x ]]; then
  RBAC_LOCAL_REPO="${RBAC_LOCAL_REPO_FROM_ENV}"
fi
if [[ "${RBAC_CONFIG_REPO_WAS_SET}" == x ]]; then
  RBAC_CONFIG_REPO="${RBAC_CONFIG_REPO_FROM_ENV}"
fi
if [[ "${INVENTORY_API_REPO_WAS_SET}" == x ]]; then
  INVENTORY_API_REPO="${INVENTORY_API_REPO_FROM_ENV}"
fi
if [[ "${HBI_REPO_WAS_SET}" == x ]]; then
  HBI_REPO="${HBI_REPO_FROM_ENV}"
fi
INVENTORY_API_REPO_FROM_CONFIG="${INVENTORY_API_REPO:-}"
HBI_REPO_FROM_CONFIG="${HBI_REPO:-}"
unset RBAC_LOCAL_REPO_WAS_SET RBAC_LOCAL_REPO_FROM_ENV RBAC_CONFIG_REPO_WAS_SET RBAC_CONFIG_REPO_FROM_ENV
unset INVENTORY_API_REPO_WAS_SET INVENTORY_API_REPO_FROM_ENV HBI_REPO_WAS_SET HBI_REPO_FROM_ENV

usage() {
  cat <<'EOF'
Usage: up-full.sh

Source selection is made by make:
  make docker-local-full-up rbac=<source> rbac-config=<source> inventory=<source> hbi=<source>

Sources:
  local         Use a local checkout (prompted if no path is supplied).
  upstream      Use the latest commit from the hard-coded upstream repository.
  <PR URL>      Fetch and use the GitHub pull request.
  <commit SHA>  Fetch and use the specified commit.

Hard-coded upstream repositories:
  RBAC          https://github.com/project-kessel/insights-rbac.git
  rbac-config   https://github.com/project-kessel/rbac-config.git
  Inventory     https://github.com/project-kessel/inventory-api.git
  HBI           https://github.com/RedHatInsights/insights-host-inventory.git

Defaults:
  RBAC          local
  rbac-config   upstream
  inventory     upstream
  hbi           upstream

When the stack is already running, the selected sources are rebuilt or
refreshed and a summary is printed.

Environment:
  RBAC_IMAGE           Docker image tag for RBAC services (default depends on source)
  INVENTORY_DB_PORT    Host port for HBI Postgres (default: 15433)
  HBI_WEB_PORT         Host port for HBI API (default: 8080)
  UNLEASH_TOKEN        Required by Host Inventory dev.yml parsing (default: local-dev-token)

Local source paths are saved in:
  ${XDG_CONFIG_HOME:-~/.config}/insights-rbac/local-stack.env
EOF
}

if [[ $# -gt 0 ]]; then
  log-err "Source selection must use make variables: rbac=<source> rbac-config=<source> inventory=<source> hbi=<source>."
  usage
  exit 1
fi

require_cmd() {
  # Verify that a required external command is available on PATH.
  if ! command -v "$1" &>/dev/null; then
    log-err "Required command not found: $1"
    exit 1
  fi
}

prompt_local_repo() {
  # Prompt the user for a local checkout path for the given service.
  # Uses the saved or default path when the terminal is non-interactive.
  # Arguments: service name, default path.
  local service="$1" default_path="$2" current_path

  case "${service}" in
    RBAC) current_path="${RBAC_LOCAL_REPO:-}" ;;
    rbac-config) current_path="${RBAC_CONFIG_REPO:-}" ;;
    inventory) current_path="${INVENTORY_API_REPO:-}" ;;
    hbi) current_path="${HBI_REPO:-}" ;;
  esac
  if [[ -n "${current_path}" && -d "${current_path}" ]]; then
    return 0
  fi
  if [[ -n "${current_path}" ]]; then
    log-warn "Saved local ${service} checkout does not exist: ${current_path}"
    current_path=""
    case "${service}" in
      RBAC) RBAC_LOCAL_REPO="" ;;
      rbac-config) RBAC_CONFIG_REPO="" ;;
      inventory) INVENTORY_API_REPO="" ;;
      hbi) HBI_REPO="" ;;
    esac
  fi

  if [[ ! -t 0 || ! -t 1 || ! -r /dev/tty ]]; then
    log-info "Using default local ${service} checkout: ${default_path}"
    return 0
  fi

  printf 'Local %s checkout [%s] (Enter to use default): ' "${service}" "${default_path}" >/dev/tty
  IFS= read -r current_path </dev/tty || current_path=""
  if [[ -z "${current_path}" ]]; then
    current_path="${default_path}"
  fi

  case "${service}" in
    RBAC) RBAC_LOCAL_REPO="${current_path}" ;;
    rbac-config) RBAC_CONFIG_REPO="${current_path}" ;;
    inventory) INVENTORY_API_REPO="${current_path}" ;;
    hbi) HBI_REPO="${current_path}" ;;
  esac
}

resolve_local_source_paths() {
  # Resolve and validate local checkout paths for all services whose
  # source kind is "local". Falls back to default paths for Inventory
  # and HBI when no local path is configured.
  if [[ "${RBAC_SOURCE_KIND}" == local ]]; then
    prompt_local_repo RBAC "${REPO_ROOT}"
    RBAC_LOCAL_REPO="$(cd "${RBAC_LOCAL_REPO:-${REPO_ROOT}}" 2>/dev/null && pwd)" || {
      log-err "RBAC local checkout is not a directory: ${RBAC_LOCAL_REPO}"
      exit 1
    }
    log-info "Using local RBAC checkout at ${RBAC_LOCAL_REPO}"
  fi

  if [[ "${RBAC_CONFIG_SOURCE}" == local ]]; then
    prompt_local_repo rbac-config "${DEFAULT_RBAC_CONFIG_REPO}"
    RBAC_CONFIG_REPO="$(cd "${RBAC_CONFIG_REPO:-${DEFAULT_RBAC_CONFIG_REPO}}" 2>/dev/null && pwd)" || {
      log-err "Local rbac-config checkout is not a directory: ${RBAC_CONFIG_REPO}"
      exit 1
    }
    log-info "Using local rbac-config checkout at ${RBAC_CONFIG_REPO}"
  fi

  if [[ "${INVENTORY_SOURCE_KIND}" == local ]]; then
    prompt_local_repo inventory "${REPO_ROOT}/.local-deps/inventory-api"
    INVENTORY_API_REPO="$(cd "${INVENTORY_API_REPO:-${REPO_ROOT}/.local-deps/inventory-api}" 2>/dev/null && pwd)" || {
      log-err "Kessel Inventory local checkout is not a directory: ${INVENTORY_API_REPO}"
      exit 1
    }
    log-info "Using local Kessel Inventory checkout at ${INVENTORY_API_REPO}"
  fi

  if [[ "${HBI_SOURCE_KIND}" == local ]]; then
    prompt_local_repo hbi "${REPO_ROOT}/.local-deps/insights-host-inventory"
    HBI_REPO="$(cd "${HBI_REPO:-${REPO_ROOT}/.local-deps/insights-host-inventory}" 2>/dev/null && pwd)" || {
      log-err "HBI local checkout is not a directory: ${HBI_REPO}"
      exit 1
    }
    log-info "Using local HBI checkout at ${HBI_REPO}"
  fi

  INVENTORY_API_REPO="${INVENTORY_API_REPO:-${REPO_ROOT}/.local-deps/inventory-api}"
  HBI_REPO="${HBI_REPO:-${REPO_ROOT}/.local-deps/insights-host-inventory}"
}

save_local_source_paths() {
  # Persist user-selected local checkout paths to the config file so
  # subsequent runs reuse them. Preserves previously saved Inventory
  # and HBI paths when their current source is not "local". Skipped
  # inside a temporary RBAC PR worktree invocation.
  [[ "${RBAC_PR_WORKTREE:-false}" != true ]] || return 0
  [[ -n "${RBAC_LOCAL_REPO:-}" ||
    -n "${RBAC_CONFIG_REPO:-}" ||
    "${INVENTORY_SOURCE_KIND}" == local ||
    "${HBI_SOURCE_KIND}" == local ]] || return 0

  local config_dir config_tmp
  local inventory_path hbi_path
  inventory_path="${INVENTORY_API_REPO:-}"
  hbi_path="${HBI_REPO:-}"
  if [[ "${INVENTORY_SOURCE_KIND}" != local ]]; then
    if [[ "${INVENTORY_API_REPO_WAS_IN_CONFIG}" == true ]]; then
      inventory_path="${INVENTORY_API_REPO_FROM_CONFIG}"
    else
      inventory_path=""
    fi
  fi
  if [[ "${HBI_SOURCE_KIND}" != local ]]; then
    if [[ "${HBI_REPO_WAS_IN_CONFIG}" == true ]]; then
      hbi_path="${HBI_REPO_FROM_CONFIG}"
    else
      hbi_path=""
    fi
  fi
  config_dir="$(dirname "${LOCAL_STACK_CONFIG_FILE}")"
  mkdir -p "${config_dir}"
  umask 077
  config_tmp="$(mktemp "${LOCAL_STACK_CONFIG_FILE}.tmp.XXXXXX")"
  {
    printf '# User-local paths for make docker-local-full-up.\n'
    printf 'RBAC_LOCAL_REPO=%q\n' "${RBAC_LOCAL_REPO:-}"
    printf 'RBAC_CONFIG_REPO=%q\n' "${RBAC_CONFIG_REPO:-}"
    printf 'INVENTORY_API_REPO=%q\n' "${inventory_path}"
    printf 'HBI_REPO=%q\n' "${hbi_path}"
  } >"${config_tmp}"
  if [[ -f "${LOCAL_STACK_CONFIG_FILE}" ]] && cmp -s "${config_tmp}" "${LOCAL_STACK_CONFIG_FILE}"; then
    rm -f "${config_tmp}"
    return 0
  fi
  mv "${config_tmp}" "${LOCAL_STACK_CONFIG_FILE}"
  log-info "Saved local source paths to ${LOCAL_STACK_CONFIG_FILE}"
}

has_only_generated_v2_openapi_conflicts() {
  local worktree="$1" conflicted_file found_conflict=false

  while IFS= read -r conflicted_file; do
    [[ -n "${conflicted_file}" ]] || continue
    case "${conflicted_file}" in
      docs/source/specs/v2/openapi.json|docs/source/specs/v2/openapi.yaml)
        found_conflict=true
        ;;
      *)
        return 1
        ;;
    esac
  done < <(git -C "${worktree}" diff --name-only --diff-filter=U)

  [[ "${found_conflict}" == true ]]
}

resolve_generated_v2_openapi_conflicts() {
  local worktree="$1"

  has_only_generated_v2_openapi_conflicts "${worktree}" || return 1
  log-warn "Resolving generated V2 OpenAPI rebase conflicts from TypeSpec source..."
  git -C "${worktree}" checkout --theirs -- docs/source/specs/v2/openapi.json docs/source/specs/v2/openapi.yaml || return 1
  git -C "${worktree}" add docs/source/specs/v2/openapi.json docs/source/specs/v2/openapi.yaml || return 1
}

regenerate_v2_openapi_spec() {
  local worktree="$1"

  log-info "Regenerating V2 OpenAPI artifacts from merged TypeSpec source..."
  make -C "${worktree}" generate_v2_spec
}

rebase_rbac_pr_worktree() {
  local worktree="$1" master_revision="$2"

  if git -C "${worktree}" rebase "${master_revision}"; then
    return 0
  fi

  while git -C "${worktree}" rebase --show-current-patch >/dev/null 2>&1; do
    if ! resolve_generated_v2_openapi_conflicts "${worktree}"; then
      return 1
    fi
    if GIT_EDITOR=true git -C "${worktree}" rebase --continue; then
      return 0
    fi
  done

  return 1
}

start_rbac_worktree() {
  # Create a temporary detached worktree for non-local RBAC sources
  # (upstream, PR, or commit SHA), copy the current orchestration
  # scripts into it, and re-invoke up-full.sh from that checkout.
  # For PR sources, rebases onto upstream master. When the PR branch
  # contains merge commits, uses merge instead of rebase to preserve
  # manual conflict resolutions.
  [[ "${RBAC_SOURCE_KIND}" != local ]] || return 0
  [[ -z "${RBAC_PR_WORKTREE:-}" ]] || return 0

  local fetch_ref repository worktree_prefix pr_revision master_revision merge_base_rev
  case "${RBAC_SOURCE_KIND}" in
    upstream)
      fetch_ref=HEAD
      worktree_prefix=upstream
      ;;
    pr)
      fetch_ref="pull/${RBAC_PR_NUMBER}/head"
      worktree_prefix="pr-${RBAC_PR_NUMBER}"
      ;;
    sha)
      fetch_ref="${RBAC_SOURCE_REF}"
      worktree_prefix="sha-${RBAC_SOURCE_REF:0:12}"
      ;;
    *)
      log-err "Unsupported RBAC source kind: ${RBAC_SOURCE_KIND}"
      exit 1
      ;;
  esac

  repository="${RBAC_UPSTREAM_REPO_URL}"
  local pr_worktree status
  pr_worktree="$(mktemp -d "${TMPDIR:-/tmp}/insights-rbac-${worktree_prefix}.XXXXXX")"
  rmdir "${pr_worktree}"
  log-info "Fetching RBAC ${RBAC_SOURCE_LABEL} from ${repository}..."
  git -C "${REPO_ROOT}" fetch --no-tags "${repository}" "${fetch_ref}"
  pr_revision="$(git -C "${REPO_ROOT}" rev-parse FETCH_HEAD)"
  git -C "${REPO_ROOT}" worktree add --detach "${pr_worktree}" "${pr_revision}" >/dev/null

  if [[ "${RBAC_SOURCE_KIND}" == pr ]]; then
    log-info "Fetching RBAC upstream master from ${repository}..."
    git -C "${REPO_ROOT}" fetch --no-tags "${repository}" master
    master_revision="$(git -C "${REPO_ROOT}" rev-parse FETCH_HEAD)"
    if [[ "$(git -C "${REPO_ROOT}" rev-parse --is-shallow-repository)" == true ]]; then
      git -C "${REPO_ROOT}" fetch --unshallow --no-tags "${repository}" || {
        log-err "Cannot unshallow ${REPO_ROOT}; merge-commit detection needs full history."
        exit 1
      }
    fi
    if ! merge_base_rev="$(git -C "${pr_worktree}" merge-base "${master_revision}" HEAD)"; then
      log-err "Cannot find a merge base between RBAC ${RBAC_SOURCE_LABEL} and upstream master."
      exit 1
    fi
    local merge_count
    if ! merge_count="$(git -C "${pr_worktree}" rev-list --merges --count "${merge_base_rev}..HEAD")"; then
      log-err "Cannot inspect RBAC ${RBAC_SOURCE_LABEL} history for merge commits."
      exit 1
    fi
    if (( merge_count > 0 )); then
      log-info "PR branch contains merge commits; using merge to preserve manual resolutions."
      git -C "${pr_worktree}" checkout --detach "${master_revision}" >/dev/null 2>&1
      if ! git -C "${pr_worktree}" merge --no-edit "${pr_revision}"; then
        log-err "RBAC ${RBAC_SOURCE_LABEL} conflicts with upstream master; stopped at ${pr_worktree}."
        exit 1
      fi
    else
      log-info "Rebasing RBAC ${RBAC_SOURCE_LABEL} onto upstream master..."
      if ! rebase_rbac_pr_worktree "${pr_worktree}" "${master_revision}"; then
        log-err "RBAC ${RBAC_SOURCE_LABEL} conflicts with upstream master; stopped at ${pr_worktree}."
        exit 1
      fi
    fi
    regenerate_v2_openapi_spec "${pr_worktree}"
  fi

  # Older PR branches may predate the local full-stack helper directory and
  # shared shell helpers. Copy the current orchestration files into the
  # temporary checkout before building and starting that PR's application.
  mkdir -p "${pr_worktree}/scripts/common"
  mkdir -p "${pr_worktree}/scripts/local_stack"
  cp "${SCRIPT_DIR}/../common/container_runtime.sh" "${pr_worktree}/scripts/common/container_runtime.sh"
  cp "${SCRIPT_DIR}/../common/logging.sh" "${pr_worktree}/scripts/common/logging.sh"
  cp "${SCRIPT_DIR}/up-full.sh" "${pr_worktree}/scripts/local_stack/up-full.sh"
  cp "${SCRIPT_DIR}/full-kessel.rbac-override.yml" \
    "${pr_worktree}/scripts/local_stack/full-kessel.rbac-override.yml"
  cp "${SCRIPT_DIR}/prepare-full-kessel-configs.sh" \
    "${pr_worktree}/scripts/local_stack/prepare-full-kessel-configs.sh"
  cp "${SCRIPT_DIR}/start-kessel-compose.sh" "${pr_worktree}/scripts/local_stack/start-kessel-compose.sh"
  cp "${SCRIPT_DIR}/ensure-hbi-kafka-topics.sh" \
    "${pr_worktree}/scripts/local_stack/ensure-hbi-kafka-topics.sh"
  cp "${SCRIPT_DIR}/extract_rbac_role_definitions.py" \
    "${pr_worktree}/scripts/local_stack/extract_rbac_role_definitions.py"
  cp "${SCRIPT_DIR}/hbi.integration.yml" "${pr_worktree}/scripts/local_stack/hbi.integration.yml"
  chmod +x "${pr_worktree}/scripts/local_stack/up-full.sh"

  log-info "Using RBAC ${RBAC_SOURCE_LABEL} checkout at ${pr_worktree}"
  if RBAC_PR_WORKTREE=true RBAC_SOURCE=local RBAC_SOURCE_LABEL="${RBAC_SOURCE_LABEL}" \
    RBAC_LOCAL_REPO="${pr_worktree}" RBAC_CONFIG_SOURCE="${RBAC_CONFIG_SOURCE}" \
    RBAC_CONFIG_REPO="${RBAC_CONFIG_REPO}" \
    INVENTORY_SOURCE="${INVENTORY_SOURCE}" INVENTORY_SOURCE_LABEL="${INVENTORY_SOURCE_LABEL}" \
    HBI_SOURCE="${HBI_SOURCE}" HBI_SOURCE_LABEL="${HBI_SOURCE_LABEL}" \
    INVENTORY_API_REPO="${INVENTORY_API_REPO}" HBI_REPO="${HBI_REPO}" \
    FULL_STACK_DEFAULT_USERS_FIXTURE="${DEFAULT_USERS_FIXTURE}" \
    FULL_STACK_DEFAULT_USERS_APPLY_SCRIPT="${DEFAULT_USERS_APPLY_SCRIPT}" \
    RBAC_IMAGE="${RBAC_IMAGE}" \
    "${pr_worktree}/scripts/local_stack/up-full.sh"; then
    status=0
  else
    status=$?
  fi

  git -C "${REPO_ROOT}" worktree remove --force "${pr_worktree}" >/dev/null 2>&1 || true
  exit "${status}"
}

select_rbac_config_pr() {
  [[ -n "${RBAC_CONFIG_PR_URL}" ]] || return 0

  local pr_number raw_base
  if [[ "${RBAC_CONFIG_PR_URL}" =~ ^https://github\.com/project-kessel/rbac-config/pull/([0-9]+)(/.*)?$ ]]; then
    pr_number="${BASH_REMATCH[1]}"
  else
    log-err "rbac-config PR URL must point to project-kessel/rbac-config: ${RBAC_CONFIG_PR_URL}"
    exit 1
  fi

  raw_base="https://raw.githubusercontent.com/project-kessel/rbac-config/refs/pull/${pr_number}/head"
  export RBAC_CONFIG_URL="${raw_base}/_private/configmaps/stage/rbac-config.yml"

  # A local generated schema is more specific than the schema committed by the PR.
  # This is useful while iterating on KSL before schema.zed has been updated.
  if [[ -z "${SCHEMA_ZED_FILE:-}" ]]; then
    export SCHEMA_ZED_URL="${raw_base}/configs/stage/schemas/schema.zed"
    log-info "Using rbac-config PR #${pr_number} stage ConfigMap and committed schema.zed"
  else
    log-info "Using rbac-config PR #${pr_number} stage ConfigMap and local generated schema"
  fi
}

select_local_rbac_config() {
  [[ -n "${RBAC_CONFIG_REPO}" ]] || return 0
  if [[ -n "${RBAC_CONFIG_PR_URL}" ]]; then
    log-err 'Use either RBAC_CONFIG_REPO or RBAC_CONFIG_PR_URL, not both.'
    exit 1
  fi

  local config_repo config_file schema_file
  config_repo="$(cd "${RBAC_CONFIG_REPO:-${DEFAULT_RBAC_CONFIG_REPO}}" 2>/dev/null && pwd)" || {
    log-err "RBAC_CONFIG_REPO is not a directory: ${RBAC_CONFIG_REPO}"
    exit 1
  }
  config_file="${config_repo}/_private/configmaps/stage/rbac-config.yml"
  [[ -f "${config_file}" ]] || {
    log-err "Stage ConfigMap not found: ${config_file}"
    exit 1
  }

  export RBAC_CONFIG_FILE="${config_file}"
  if [[ -z "${SCHEMA_ZED_FILE:-}" ]]; then
    schema_file="${config_repo}/_private/test-schema/stage-schema.zed"
    log-info "Building local rbac-config stage schema..."
    make -C "${config_repo}" ksl-test-schema-stage
    [[ -f "${schema_file}" ]] || {
      log-err "Generated stage schema not found: ${schema_file}"
      exit 1
    }
    export SCHEMA_ZED_FILE="${schema_file}"
    log-info "Using local rbac-config checkout at ${config_repo} and its generated stage schema"
  else
    log-info "Using local rbac-config ConfigMap and explicit local schema"
  fi
}

is_commit_sha() {
  [[ "${1}" =~ ^[0-9a-fA-F]{7,64}$ ]]
}

select_rbac_source() {
  case "${RBAC_SOURCE}" in
    local)
      RBAC_SOURCE_KIND=local
      RBAC_IMAGE="${RBAC_IMAGE:-insights-rbac-local:dev}"
      ;;
    upstream)
      RBAC_SOURCE_KIND=upstream
      RBAC_IMAGE="${RBAC_IMAGE:-insights-rbac-local:dev}"
      ;;
    https://github.com/*/pull/[0-9]*|https://github.com/*/pull/[0-9]*/*)
      RBAC_SOURCE_KIND=pr
      RBAC_PR_URL="${RBAC_SOURCE}"
      if [[ "${RBAC_PR_URL}" =~ ^https://github\.com/project-kessel/insights-rbac/pull/([0-9]+)(/.*)?$ ]]; then
        RBAC_PR_NUMBER="${BASH_REMATCH[1]}"
      else
        log-err "RBAC PR URL must point to project-kessel/insights-rbac: ${RBAC_SOURCE}"
        exit 1
      fi
      RBAC_IMAGE="${RBAC_IMAGE:-insights-rbac-pr-${RBAC_PR_NUMBER}:dev}"
      ;;
    *)
      if is_commit_sha "${RBAC_SOURCE}"; then
        RBAC_SOURCE_KIND=sha
        RBAC_SOURCE_REF="${RBAC_SOURCE}"
        RBAC_IMAGE="${RBAC_IMAGE:-insights-rbac-sha-${RBAC_SOURCE:0:12}:dev}"
        return 0
      fi
      log-err "RBAC source must be local, upstream, a GitHub pull request URL, or a commit SHA: ${RBAC_SOURCE}"
      usage
      exit 1
      ;;
  esac
}

select_rbac_config_source() {
  case "${RBAC_CONFIG_SOURCE}" in
    upstream)
      RBAC_CONFIG_SOURCE_KIND=upstream
      export RBAC_CONFIG_URL="https://raw.githubusercontent.com/project-kessel/rbac-config/refs/heads/master/_private/configmaps/stage/rbac-config.yml"
      export SCHEMA_ZED_URL="https://raw.githubusercontent.com/project-kessel/rbac-config/refs/heads/master/configs/stage/schemas/schema.zed"
      log-info "Using upstream rbac-config stage configuration from ${RBAC_CONFIG_UPSTREAM_REPO_URL}."
      ;;
    local)
      RBAC_CONFIG_SOURCE_KIND=local
      select_local_rbac_config
      ;;
    https://github.com/*/pull/[0-9]*|https://github.com/*/pull/[0-9]*/*)
      RBAC_CONFIG_SOURCE_KIND=pr
      RBAC_CONFIG_PR_URL="${RBAC_CONFIG_SOURCE}"
      select_rbac_config_pr
      ;;
    *)
      if is_commit_sha "${RBAC_CONFIG_SOURCE}"; then
        RBAC_CONFIG_SOURCE_KIND=sha
        RBAC_CONFIG_SOURCE_REF="${RBAC_CONFIG_SOURCE}"
        export RBAC_CONFIG_URL="https://raw.githubusercontent.com/project-kessel/rbac-config/${RBAC_CONFIG_SOURCE}/_private/configmaps/stage/rbac-config.yml"
        export SCHEMA_ZED_URL="https://raw.githubusercontent.com/project-kessel/rbac-config/${RBAC_CONFIG_SOURCE}/configs/stage/schemas/schema.zed"
        log-info "Using rbac-config commit ${RBAC_CONFIG_SOURCE}."
        return 0
      fi
      log-err "rbac-config source must be local, upstream, a GitHub pull request URL, or a commit SHA: ${RBAC_CONFIG_SOURCE}"
      usage
      exit 1
      ;;
  esac
}

select_inventory_source() {
  # Parse the INVENTORY_SOURCE value and set INVENTORY_SOURCE_KIND,
  # INVENTORY_SOURCE_REPO, INVENTORY_PR_NUMBER, or INVENTORY_SOURCE_REF.
  case "${INVENTORY_SOURCE}" in
    local)
      INVENTORY_SOURCE_KIND=local
      ;;
    upstream)
      INVENTORY_SOURCE_KIND=upstream
      ;;
    https://github.com/*/pull/[0-9]*|https://github.com/*/pull/[0-9]*/*)
      if [[ "${INVENTORY_SOURCE}" =~ ^https://github\.com/([^/]+/[^/]+)/pull/([0-9]+)(/.*)?$ ]]; then
        INVENTORY_SOURCE_KIND=pr
        INVENTORY_SOURCE_REPO="https://github.com/${BASH_REMATCH[1]}.git"
        INVENTORY_PR_NUMBER="${BASH_REMATCH[2]}"
      else
        log-err "Kessel Inventory PR source is not a valid GitHub pull request URL: ${INVENTORY_SOURCE}"
        exit 1
      fi
      ;;
    *)
      if is_commit_sha "${INVENTORY_SOURCE}"; then
        INVENTORY_SOURCE_KIND=sha
        INVENTORY_SOURCE_REF="${INVENTORY_SOURCE}"
      else
        log-err "inventory source must be local, upstream, a GitHub pull request URL, or a commit SHA: ${INVENTORY_SOURCE}"
        usage
        exit 1
      fi
      ;;
  esac
}

select_hbi_source() {
  # Parse the HBI_SOURCE value and set HBI_SOURCE_KIND,
  # HBI_SOURCE_REPO, HBI_PR_NUMBER, or HBI_SOURCE_REF.
  case "${HBI_SOURCE}" in
    local)
      HBI_SOURCE_KIND=local
      ;;
    upstream)
      HBI_SOURCE_KIND=upstream
      ;;
    https://github.com/*/pull/[0-9]*|https://github.com/*/pull/[0-9]*/*)
      if [[ "${HBI_SOURCE}" =~ ^https://github\.com/([^/]+/[^/]+)/pull/([0-9]+)(/.*)?$ ]]; then
        HBI_SOURCE_KIND=pr
        HBI_SOURCE_REPO="https://github.com/${BASH_REMATCH[1]}.git"
        HBI_PR_NUMBER="${BASH_REMATCH[2]}"
      else
        log-err "HBI PR source is not a valid GitHub pull request URL: ${HBI_SOURCE}"
        exit 1
      fi
      ;;
    *)
      if is_commit_sha "${HBI_SOURCE}"; then
        HBI_SOURCE_KIND=sha
        HBI_SOURCE_REF="${HBI_SOURCE}"
      else
        log-err "hbi source must be local, upstream, a GitHub pull request URL, or a commit SHA: ${HBI_SOURCE}"
        usage
        exit 1
      fi
      ;;
  esac
}

stack_is_running() {
  # Return true when the full Kessel RBAC server container is running.
  [[ "$("${CONTAINER_RUNTIME}" container inspect --format '{{.State.Running}}' full-kessel-rbac-server-1 2>/dev/null || true)" == true ]]
}

print_source_summary() {
  # Log the selected deployment sources and note when an existing
  # stack is being rebuilt.
  log-info "Deployment sources: RBAC=${RBAC_SOURCE_LABEL}, rbac-config=${RBAC_CONFIG_SOURCE_LABEL}, HBI=${HBI_SOURCE}, Kessel Inventory=${INVENTORY_SOURCE}."
  if [[ "${STACK_WAS_RUNNING}" == true ]]; then
    log-info 'Existing Docker stack detected; rebuilding or refreshing services for the selected sources.'
  fi
}

SOURCE_WORKTREE_PATHS=()
SOURCE_WORKTREE_BASES=()

cleanup_source_worktrees() {
  # Remove all temporary git worktrees created for Inventory or HBI
  # non-local sources. Registered as a trap handler.
  local index
  for index in "${!SOURCE_WORKTREE_PATHS[@]}"; do
    git -C "${SOURCE_WORKTREE_BASES[${index}]}" worktree remove --force "${SOURCE_WORKTREE_PATHS[${index}]}" >/dev/null 2>&1 || true
  done
}

create_source_worktree() {
  # Fetch a ref from a remote repository and create a temporary
  # detached worktree for it. Updates the corresponding repo variable
  # (INVENTORY_API_REPO or HBI_REPO) and registers the worktree for
  # cleanup. Arguments: service, base_repo, source_repo, fetch_ref,
  # worktree_prefix.
  local service="$1" base_repo="$2" source_repo="$3" fetch_ref="$4" worktree_prefix="$5"
  local worktree

  worktree="$(mktemp -d "${TMPDIR:-/tmp}/insights-rbac-${worktree_prefix}.XXXXXX")"
  rmdir "${worktree}"
  log-info "Fetching ${service} source from ${source_repo} (${fetch_ref})..."
  git -C "${base_repo}" fetch --no-tags "${source_repo}" "${fetch_ref}"
  git -C "${base_repo}" worktree add --detach "${worktree}" FETCH_HEAD >/dev/null
  SOURCE_WORKTREE_BASES+=("${base_repo}")
  SOURCE_WORKTREE_PATHS+=("${worktree}")

  case "${service}" in
    inventory) INVENTORY_API_REPO="${worktree}" ;;
    hbi) HBI_REPO="${worktree}" ;;
  esac
  log-info "Using ${service} checkout at ${worktree}"
}

ensure_inventory_api_repo() {
  # Ensure the Kessel Inventory checkout exists and contains the
  # required start-full-kessel.sh script. Clones upstream when the
  # checkout is absent and the source is not local.
  if [[ ! -f "${INVENTORY_API_REPO}/scripts/start-full-kessel.sh" ]]; then
    if [[ "${INVENTORY_SOURCE_KIND}" == local ]]; then
      log-err "Local Kessel Inventory checkout is incomplete: ${INVENTORY_API_REPO}"
      exit 1
    fi
    if [[ -e "${INVENTORY_API_REPO}" ]]; then
      log-err "Upstream inventory-api checkout is incomplete: ${INVENTORY_API_REPO}"
      exit 1
    fi
    log-info "Cloning upstream inventory-api into ${INVENTORY_API_REPO}..."
    mkdir -p "$(dirname "${INVENTORY_API_REPO}")"
    git clone --depth 1 "${INVENTORY_API_UPSTREAM_REPO_URL}" "${INVENTORY_API_REPO}"
  fi
}

ensure_hbi_repo() {
  # Ensure the Host Inventory checkout exists and contains dev.yml.
  # Clones upstream when the checkout is absent and the source is not
  # local.
  if [[ ! -f "${HBI_REPO}/dev.yml" ]]; then
    if [[ "${HBI_SOURCE_KIND}" == local ]]; then
      log-err "Local HBI checkout is incomplete: ${HBI_REPO}"
      exit 1
    fi
    if [[ -e "${HBI_REPO}" ]]; then
      log-err "Upstream insights-host-inventory checkout is incomplete: ${HBI_REPO}"
      exit 1
    fi
    log-info "Cloning upstream insights-host-inventory into ${HBI_REPO}..."
    mkdir -p "$(dirname "${HBI_REPO}")"
    git clone --depth 1 "${HBI_UPSTREAM_REPO_URL}" "${HBI_REPO}"
  fi
}

prepare_inventory_api_source() {
  # Prepare the Kessel Inventory source according to
  # INVENTORY_SOURCE_KIND: validate the local checkout, pull upstream,
  # or create a temporary worktree for a PR or commit SHA.
  ensure_inventory_api_repo
  case "${INVENTORY_SOURCE_KIND}" in
    local)
      log-info "Using local Kessel Inventory checkout at ${INVENTORY_API_REPO}"
      ;;
    upstream)
      pull_repository "inventory-api" "${INVENTORY_API_REPO}" "${INVENTORY_API_UPSTREAM_REPO_URL}"
      log-info "Using upstream Kessel Inventory checkout at ${INVENTORY_API_REPO}"
      ;;
    pr)
      create_source_worktree inventory "${INVENTORY_API_REPO}" "${INVENTORY_SOURCE_REPO}" \
        "pull/${INVENTORY_PR_NUMBER}/head" "inventory-pr-${INVENTORY_PR_NUMBER}"
      ;;
    sha)
      create_source_worktree inventory "${INVENTORY_API_REPO}" "${INVENTORY_API_UPSTREAM_REPO_URL}" \
        "${INVENTORY_SOURCE_REF}" "inventory-sha-${INVENTORY_SOURCE_REF:0:12}"
      ;;
  esac
}

prepare_hbi_source() {
  # Prepare the Host Inventory source according to HBI_SOURCE_KIND:
  # validate the local checkout, pull upstream, or create a temporary
  # worktree for a PR or commit SHA.
  ensure_hbi_repo
  case "${HBI_SOURCE_KIND}" in
    local)
      log-info "Using local HBI checkout at ${HBI_REPO}"
      ;;
    upstream)
      pull_repository "insights-host-inventory" "${HBI_REPO}" "${HBI_UPSTREAM_REPO_URL}"
      log-info "Using upstream HBI checkout at ${HBI_REPO}"
      ;;
    pr)
      create_source_worktree hbi "${HBI_REPO}" "${HBI_SOURCE_REPO}" \
        "pull/${HBI_PR_NUMBER}/head" "hbi-pr-${HBI_PR_NUMBER}"
      ;;
    sha)
      create_source_worktree hbi "${HBI_REPO}" "${HBI_UPSTREAM_REPO_URL}" \
        "${HBI_SOURCE_REF}" "hbi-sha-${HBI_SOURCE_REF:0:12}"
      ;;
  esac
}

initialize_hbi_submodules() {
  # Initialize and update git submodules in the HBI checkout.
  log-info "Initializing Host Inventory git submodules..."
  git -C "${HBI_REPO}" submodule update --init --recursive
}

pull_repository() {
  local name="$1"
  local repository="$2"
  local upstream_url="$3"

  log-info "Updating upstream ${name} from ${upstream_url}..."
  git -C "${repository}" fetch --no-tags "${upstream_url}" HEAD
  git -C "${repository}" merge --ff-only FETCH_HEAD
}

start_kessel_stack() {
  export RBAC_IMAGE
  export COMPOSE_PULL_MODE
  export DOCKER="${CONTAINER_RUNTIME}"
  export RBAC_FORCE_RECREATE
  export STACK_WAS_RUNNING
  log-info "Building and starting Kessel + Debezium + RBAC (RBAC_IMAGE=${RBAC_IMAGE})..."
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

load_default_users() {
  [[ "${FULL_STACK_LOAD_DEFAULT_USERS:-true}" == true ]] || return 0
  [[ -f "${DEFAULT_USERS_FIXTURE}" ]] || {
    log-err "Default full-stack user fixture not found: ${DEFAULT_USERS_FIXTURE}"
    return 1
  }
  [[ -x "${DEFAULT_USERS_APPLY_SCRIPT}" ]] || {
    log-err "Default user loader is not executable: ${DEFAULT_USERS_APPLY_SCRIPT}"
    return 1
  }

  log-info "Waiting for RBAC API before loading default full-stack users..."
  local attempt
  for attempt in $(seq 1 60); do
    if curl -fsS http://localhost:9080/metrics >/dev/null 2>&1; then
      log-info "Loading default V1/V2 local users from ${DEFAULT_USERS_FIXTURE}..."
      CONTAINER_RUNTIME="${CONTAINER_RUNTIME}" \
        RBAC_SERVER_CONTAINER=full-kessel-rbac-server-1 \
        "${DEFAULT_USERS_APPLY_SCRIPT}" --file "${DEFAULT_USERS_FIXTURE}"
      return 0
    fi
    sleep 2
  done

  log-err 'RBAC API did not become ready; default users were not loaded.'
  return 1
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
  ./scripts/validations/api/create-workspace.sh --no-start

Verify a workspace permission (replace <workspace-uuid>):
  ./scripts/zed_local.sh check rbac/workspace:<workspace-uuid> view rbac/principal:redhat/1111111

EOF
}

require_cmd curl
require_cmd git
trap cleanup_source_worktrees EXIT

select_rbac_source
select_inventory_source
select_hbi_source
resolve_local_source_paths
save_local_source_paths
start_rbac_worktree
select_rbac_config_source

detect_container_runtime
if stack_is_running; then
  STACK_WAS_RUNNING=true
fi
print_source_summary

prepare_inventory_api_source

log-info "Building local RBAC image ${RBAC_IMAGE}..."
"${CONTAINER_RUNTIME}" build -t "${RBAC_IMAGE}" "${RBAC_LOCAL_REPO:-${REPO_ROOT}}"
export RBAC_FORCE_RECREATE=true

start_kessel_stack

prepare_hbi_source
initialize_hbi_submodules
start_hbi
load_default_users

if [[ "${STACK_WAS_RUNNING}" == true ]]; then
  log-info 'Existing Docker stack rebuilt for the selected sources.'
else
  log-info 'Full local stack started.'
fi
print_endpoints
