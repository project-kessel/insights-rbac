#!/usr/bin/env bash
# Prepare schema.zed and RBAC role definitions for inventory-api full-kessel stack.
# Called by start-kessel-compose.sh before compose up.
set -euo pipefail

prepare_full_kessel_configs() {
  local inventory_api_repo="${1:?inventory-api repo path required}"
  local repo_root="${2:?insights-rbac repo root required}"

  local compose_dir="${3:-${inventory_api_repo}/development/full-kessel}"
  local env_file="${compose_dir}/.env"
  local requested_rbac_image="${RBAC_IMAGE:-}"
  local requested_schema_zed_file="${SCHEMA_ZED_FILE:-}"
  local requested_schema_zed_url="${SCHEMA_ZED_URL:-}"
  local requested_rbac_config_file="${RBAC_CONFIG_FILE:-}"
  local requested_rbac_config_url="${RBAC_CONFIG_URL:-}"

  if [[ -f "${env_file}" ]]; then
    set -a
    # shellcheck disable=SC1090
    source "${env_file}"
    set +a
  fi
  if [[ -n "${requested_rbac_image}" ]]; then
    export RBAC_IMAGE="${requested_rbac_image}"
  fi
  # inventory-api's .env provides stage defaults.  Preserve a caller-selected
  # rbac-config PR or local generated schema instead of letting those defaults
  # replace it while loading the Compose environment.
  [[ -n "${requested_schema_zed_file}" ]] && export SCHEMA_ZED_FILE="${requested_schema_zed_file}"
  [[ -n "${requested_schema_zed_url}" ]] && export SCHEMA_ZED_URL="${requested_schema_zed_url}"
  [[ -n "${requested_rbac_config_file}" ]] && export RBAC_CONFIG_FILE="${requested_rbac_config_file}"
  [[ -n "${requested_rbac_config_url}" ]] && export RBAC_CONFIG_URL="${requested_rbac_config_url}"

  local schema_dest="${compose_dir}/configs/schema.zed"
  local local_config_dir="${TMPDIR:-/tmp}/insights-rbac-full-kessel"
  if [[ -n "${FULL_KESSEL_COMPOSE_DIR:-}" ]]; then
    local_config_dir="${compose_dir}/generated"
  fi
  local local_inventory_config

  mkdir -p "${local_config_dir}"
  local_config_dir="$(cd "${local_config_dir}" && pwd)"
  local_inventory_config="${local_config_dir}/inventory-api.yaml"
  awk '
    /^authn:$/ {
      print
      print "  allow-unauthenticated: true"
      next
    }
    { print }
  ' "${compose_dir}/configs/inventory-api.yaml" > "${local_inventory_config}"
  export RBAC_INVENTORY_API_CONFIG="${local_inventory_config}"

  if [[ -n "${SCHEMA_ZED_FILE:-}" ]]; then
    log-info "Using local schema file: ${SCHEMA_ZED_FILE}"
    cp "${SCHEMA_ZED_FILE}" "${schema_dest}"
  else
    local schema_url="${SCHEMA_ZED_URL:-https://raw.githubusercontent.com/project-kessel/rbac-config/refs/heads/master/configs/stage/schemas/schema.zed}"
    log-info "Downloading schema.zed from ${schema_url}"
    curl -fsSL -o "${schema_dest}" "${schema_url}"
  fi

  local rbac_defs_dir="${compose_dir}/configs/rbac-role-definitions"
  mkdir -p "${rbac_defs_dir}"
  rm -f "${rbac_defs_dir}"/*.json 2>/dev/null || true

  local rbac_config_src
  local _tmp_rbac_config=""
  if [[ -n "${RBAC_CONFIG_FILE:-}" ]]; then
    log-info "Using local RBAC config: ${RBAC_CONFIG_FILE}"
    rbac_config_src="${RBAC_CONFIG_FILE}"
  else
    local rbac_config_url="${RBAC_CONFIG_URL:-https://raw.githubusercontent.com/project-kessel/rbac-config/refs/heads/master/_private/configmaps/stage/rbac-config.yml}"
    _tmp_rbac_config="$(mktemp)"
    rbac_config_src="${_tmp_rbac_config}"
    log-info "Downloading RBAC role definitions from ${rbac_config_url}"
    curl -fsSL -o "${rbac_config_src}" "${rbac_config_url}"
  fi

  extract_rbac_role_definitions "${rbac_config_src}" "${rbac_defs_dir}" "${repo_root}"
  [[ -n "${_tmp_rbac_config}" ]] && rm -f "${_tmp_rbac_config}"
  log-info "Extracted $(find "${rbac_defs_dir}" -maxdepth 1 -name '*.json' | wc -l | tr -d ' ') RBAC role definition files"
}

extract_rbac_role_definitions() {
  local config_src="$1"
  local defs_dir="$2"
  local repo_root="$3"

  if command -v yq &>/dev/null; then
    log-info "Extracting RBAC role definitions with yq"
    local key
    for key in $(yq '.objects[0].data | keys | .[]' "${config_src}"); do
      yq -r ".objects[0].data[\"${key}\"]" "${config_src}" > "${defs_dir}/${key}"
    done
    return 0
  fi

  if python3 "${SCRIPT_DIR}/extract_rbac_role_definitions.py" "${config_src}" "${defs_dir}"; then
    return 0
  fi

  if command -v pipenv &>/dev/null && (
    cd "${repo_root}" && pipenv run python "${SCRIPT_DIR}/extract_rbac_role_definitions.py" "${config_src}" "${defs_dir}"
  ); then
    return 0
  fi

  log-info "yq and PyYAML not found; extracting RBAC role definitions with containerized yq"
  "${CONTAINER_RUNTIME}" run --rm \
    -v "${config_src}:/in.yml:ro" \
    -v "${defs_dir}:/out" \
    docker.io/mikefarah/yq:4 \
    sh -c 'for key in $(yq ".objects[0].data | keys | .[]" /in.yml); do
      yq -r ".objects[0].data[\"${key}\"]" /in.yml > "/out/${key}"
    done'
}
