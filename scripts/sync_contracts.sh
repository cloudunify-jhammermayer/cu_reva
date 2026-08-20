#!/usr/bin/env bash
# Vendor the generated Odoo<->REVA contracts into the consuming Odoo repo
# (Cloudunify/ since ast-odoo was retired on 2026-08-12).
# Usage: scripts/sync_contracts.sh /path/to/odoo-repo
set -euo pipefail

[ $# -eq 1 ] || { echo "usage: $0 <odoo-repo-path>"; exit 2; }

SRC="$(cd "$(dirname "$0")/.." && pwd)/contracts"
DEST="$1/reva_contracts"

[ -d "$SRC" ] || {
  echo "contracts/ missing; run: python -m reva.odoo_contracts generate"
  exit 1
}

rsync -a --delete "$SRC/" "$DEST/"
VERSION="$(python3 -c "import json;print(json.load(open('$SRC/manifest.json'))['contracts_version'][:12])")"
echo "Synced contracts_version ${VERSION}... to $DEST; review + commit there,"
echo "and bump the version pin in the addon's contract tests."
