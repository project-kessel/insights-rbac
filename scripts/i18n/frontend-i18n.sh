#!/usr/bin/env bash
set -euo pipefail

if [[ -n "${I18N_TOOLING_ROOT:-}" ]]; then
  cli="$I18N_TOOLING_ROOT/packages/i18n-pipeline/dist/cli.js"
  if [[ ! -f "$cli" ]]; then
    echo "frontend-i18n CLI not found at $cli; build i18n-pipeline in I18N_TOOLING_ROOT first" >&2
    exit 1
  fi
  exec node "$cli" "$@"
fi

if command -v frontend-i18n >/dev/null 2>&1; then
  exec frontend-i18n "$@"
fi

echo "frontend-i18n is not installed; set I18N_TOOLING_ROOT to an i18n-tooling checkout or add frontend-i18n to PATH" >&2
exit 1
