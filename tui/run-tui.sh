#!/usr/bin/env bash
# Launch the REVA TUI against the local API (via nginx on loopback :80).
# Passes any extra args straight through, e.g. `./run-tui.sh --demo`.
set -euo pipefail

cd "$(dirname "$0")"

export REVA_API_URL="${REVA_API_URL:-http://localhost/api/v1}"
if [[ -z "${REVA_API_KEY:-}" ]]; then
  export REVA_API_KEY="$(cat ../secrets/reva_api_key)"
fi

exec ./reva-tui "$@"
