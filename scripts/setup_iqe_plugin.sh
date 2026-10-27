#!/usr/bin/env bash

set -euo pipefail

: "${HOME:?HOME must be set}"

IQE_VENV_DIR="$HOME/Venvs/iqe-rbac-plugin"
IQE_RBAC_PLUGIN_DIR="${IQE_RBAC_PLUGIN_DIR:-$HOME/Projects/iqe-rbac-plugin}"
IQE_ENV="${IQE_ENV:-smoke}"
NEXUS_SIMPLE_INDEX_URL="https://nexus.corp.redhat.com/repository/cqt-pypi/simple"
NEXUS_PYPI_INDEX_URL="https://nexus.corp.redhat.com/repository/cqt-pypi/pypi/"

usage() {
    cat <<EOF
Usage:
  $0 [setup]
  $0 test
  $0 filter <pytest-filter>

IQE_ENV defaults to smoke (local Docker); set IQE_ENV=stage_proxy for stage.
IQE_RBAC_PLUGIN_DIR defaults to $HOME/Projects/iqe-rbac-plugin; set it to use another checkout path.
PYTEST_ADDOPTS defaults to -x; set it to override pytest options.
Test runs raise the process open-file limit to at least 1024.

setup   Recreate the venv, install IQE Core and the editable plugin.
test    Run all plugin tests in the selected IQE environment.
filter  Run matching tests in the selected IQE environment.
EOF
}

fail() {
    echo "Error: $*" >&2
    exit 1
}

activate_iqe_venv() {
    [[ -f "$IQE_VENV_DIR/bin/activate" ]] || fail "Virtualenv not found at $IQE_VENV_DIR; run '$0 setup' first."
    # shellcheck disable=SC1091
    source "$IQE_VENV_DIR/bin/activate"
}

configure_file_descriptor_limit() {
    local minimum=1024
    local current_limit
    current_limit="$(ulimit -S -n)"

    if [[ "$current_limit" == "unlimited" ]] || (( current_limit >= minimum )); then
        return
    fi

    # Concurrent IQE clients need sockets and thread-pool pipes. macOS's
    # 256-file soft limit can also surface as localhost name-resolution errors.
    if ! ulimit -S -n "$minimum" 2>/dev/null; then
        fail "IQE concurrency tests need an open-file limit of at least $minimum (soft=$current_limit, hard=$(ulimit -H -n)). Raise the hard limit before running tests."
    fi
    echo "Raised process open-file limit: $current_limit -> $minimum"
}

configure_requests_ca_bundle() {
    if [[ -n "${REQUESTS_CA_BUNDLE:-}" && -f "$REQUESTS_CA_BUNDLE" ]]; then
        return
    fi

    if [[ -n "${REQUESTS_CA_BUNDLE:-}" ]]; then
        echo "Warning: REQUESTS_CA_BUNDLE does not exist: $REQUESTS_CA_BUNDLE" >&2
    fi

    for ca_bundle in /etc/pki/tls/certs/ca-bundle.crt /etc/ssl/cert.pem; do
        if [[ -f "$ca_bundle" ]]; then
            export REQUESTS_CA_BUNDLE="$ca_bundle"
            echo "Using CA bundle: $REQUESTS_CA_BUNDLE"
            return
        fi
    done

    unset REQUESTS_CA_BUNDLE
    echo "No system CA bundle found; using the Python certificate defaults."
}

setup() {
    local constraints_file

    [[ -d "$IQE_RBAC_PLUGIN_DIR" ]] \
        || fail "IQE RBAC plugin checkout not found at $IQE_RBAC_PLUGIN_DIR; place the checkout there and rerun setup."
    git -C "$IQE_RBAC_PLUGIN_DIR" rev-parse --is-inside-work-tree >/dev/null 2>&1 \
        || fail "$IQE_RBAC_PLUGIN_DIR exists but is not a Git checkout."

    echo "Recreating virtualenv at $IQE_VENV_DIR"
    mkdir -p "$HOME/Venvs"
    rm -rf -- "$IQE_VENV_DIR"
    python3 -m venv "$IQE_VENV_DIR"
    # shellcheck disable=SC1091
    source "$IQE_VENV_DIR/bin/activate"
    configure_requests_ca_bundle

    pip install pytest-sugar

    pip config set global.index-url "$NEXUS_SIMPLE_INDEX_URL" --site
    pip config set global.index "$NEXUS_PYPI_INDEX_URL" --site

    pip install pre-commit
    pip install iqe-core
    constraints_file="$(python - <<'PY'
from importlib.util import find_spec
from pathlib import Path

spec = find_spec("iqe")
if spec is None or not spec.submodule_search_locations:
    raise SystemExit("Cannot locate the installed IQE Core package")
constraints_path = Path(next(iter(spec.submodule_search_locations))) / "data/all_external_constraints.linux.python3.12.txt"
print(constraints_path)
PY
)"
    [[ -f "$constraints_file" ]] || fail "IQE Core dependency constraints not found at $constraints_file."
    # The plugin imports tenacity from its tests but does not declare it in pyproject.toml.
    pip install tenacity -c "$constraints_file"
    pip install iqe-goatee-plugin

    # iqe plugin install delegates dependency resolution to uv, which reads UV_INDEX_URL.
    export UV_INDEX_URL="$NEXUS_SIMPLE_INDEX_URL"
    # Homebrew uv 0.8.x uses UV_NATIVE_TLS; IQE Core's UV_SYSTEM_CERTS needs uv 0.11+.
    export UV_NATIVE_TLS=true
    iqe plugin install --editable "$IQE_RBAC_PLUGIN_DIR"
    iqe plugin list

    cat <<EOF

Setup complete. Activate the environment in your current shell with:
  source "$IQE_VENV_DIR/bin/activate"

Run plugin tests with:
  $0 test

Run a filtered test selection with:
  $0 filter '<pytest-filter>'
EOF
}

run_plugin_tests() {
    activate_iqe_venv
    configure_file_descriptor_limit
    configure_requests_ca_bundle
    export PYTEST_ADDOPTS="${PYTEST_ADDOPTS:--x}"
    export UV_INDEX_URL="$NEXUS_SIMPLE_INDEX_URL"
    export UV_NATIVE_TLS=true

    if [[ "$IQE_ENV" == "smoke" ]]; then
        command -v curl >/dev/null 2>&1 || fail "curl is required to check the local RBAC API."
        if ! curl --noproxy '*' --silent --show-error --connect-timeout 2 --max-time 3 \
            --output /dev/null http://localhost:9080/; then
            fail "Local RBAC API is not reachable at http://localhost:9080. Start the local stack with 'make docker-local-full-up rbac=local rbac-config=local'."
        fi

        # IQE's smoke profile targets services inside the Docker network by
        # their Compose hostname. The test runner runs on the host, so use the
        # published localhost port. Smoke users use identity headers and need
        # no Vault-backed credentials.
        export DYNACONF_SERVICE_OBJECTS__RBAC__CONFIG__HOSTNAME=localhost
        # The legacy generated v1 client uses its own service object.
        export DYNACONF_SERVICE_OBJECTS__RBAC_V1__CONFIG__SCHEME=http
        export DYNACONF_SERVICE_OBJECTS__RBAC_V1__CONFIG__HOSTNAME=localhost
        export DYNACONF_SERVICE_OBJECTS__RBAC_V1__CONFIG__PORT=9080
        export DYNACONF_SERVICE_OBJECTS__RBAC_V2__CONFIG__HOSTNAME=localhost
        export DYNACONF_IQE_VAULT_LOADER_ENABLED=false
        # The IQE smoke config inherits default Jira Vault references. Local
        # runs do not need Jira credentials, so override them to avoid lazy
        # resolution attempts against the disabled Vault loader.
        export DYNACONF_JIRA__USERNAME=""
        export DYNACONF_JIRA__TOKEN=""
        export NO_PROXY="${NO_PROXY:+$NO_PROXY,}localhost,127.0.0.1,::1"
        export no_proxy="${no_proxy:+$no_proxy,}localhost,127.0.0.1,::1"
    fi

    export ENV_FOR_DYNACONF="$IQE_ENV"
    iqe tests plugin rbac "$@"
}

mode="${1:-setup}"
case "$mode" in
    setup)
        [[ $# -le 1 ]] || { usage >&2; exit 2; }
        setup
        ;;
    test)
        [[ $# -eq 1 ]] || { usage >&2; exit 2; }
        run_plugin_tests
        ;;
    filter)
        [[ $# -eq 2 ]] || { usage >&2; exit 2; }
        run_plugin_tests -k "$2"
        ;;
    -h|--help|help)
        usage
        ;;
    *)
        usage >&2
        exit 2
        ;;
esac
