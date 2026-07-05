#!/usr/bin/env bash
# Provision /core worktrees + registry for the core-knowledge layer.
#
# Runs on the host. One-time operator prereqs:
#   git clone --no-checkout https://github.com/odoo/odoo          "$CLONES/odoo"
#   git clone --no-checkout <enterprise-remote>                    "$CLONES/enterprise"
#   git clone --no-checkout https://github.com/odoo/documentation  "$CLONES/documentation"
#
# Usage: scripts/core_sync.sh 17.0 18.0 19.0
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd -- "$SCRIPT_DIR/.." && pwd)"
if [ -f "$PROJECT_ROOT/.env" ]; then
  set -a
  # shellcheck disable=SC1091
  . "$PROJECT_ROOT/.env"
  set +a
fi

CORE="${REVA_CORE_HOST_DIR:-/srv/reva-core}"
CLONES="${REVA_CORE_CLONES:-/srv/odoo-mirrors}"
COMPOSE_FILE="${COMPOSE_FILE:-$PROJECT_ROOT/docker-compose.prod.yml}"
SYNC_ENTERPRISE="${REVA_CORE_SYNC_ENTERPRISE:-true}"
SYNC_FETCH="${REVA_CORE_SYNC_FETCH:-true}"

[ "$#" -ge 1 ] || { echo "usage: $0 <version> [version...]" >&2; exit 2; }

sync_worktree() {
  local repo="$1" version="$2" dest="$3"
  shift 3
  if [ "$SYNC_FETCH" = "true" ]; then
    git -C "$CLONES/$repo" fetch origin "$version"
  else
    echo "  using existing $repo origin/$version because REVA_CORE_SYNC_FETCH=$SYNC_FETCH"
  fi
  if [ ! -d "$dest" ]; then
    git -C "$CLONES/$repo" worktree add --no-checkout "$dest" "origin/$version"
    git -C "$dest" sparse-checkout init --no-cone
    printf '%s\n' "$@" | git -C "$dest" sparse-checkout set --no-cone --stdin
  fi
  git -C "$dest" checkout -f "origin/$version"
  echo "  synced $repo@$version -> $dest"
}

for version in "$@"; do
  echo "== core knowledge: $version =="
  vdir="$CORE/$version"
  mkdir -p "$vdir"

  sync_worktree odoo "$version" "$vdir/odoo" \
    '/*' '!**/i18n/' '!**/*.po' '!**/*.pot'
  if [ "$SYNC_ENTERPRISE" = "true" ]; then
    sync_worktree enterprise "$version" "$vdir/enterprise" \
      '/*' '!**/i18n/' '!**/*.po' '!**/*.pot'
  else
    echo "  skipped enterprise@$version because REVA_CORE_SYNC_ENTERPRISE=$SYNC_ENTERPRISE"
  fi
  sync_worktree documentation "$version" "$vdir/documentation" \
    '/content/' '!**/*.png' '!**/*.gif' '!/locale/'

  docker compose -f "$COMPOSE_FILE" exec -T worker \
    python -m reva.odoo_registry load "/core/$version" --version "$version"
done

echo "Done. Restart the worker if REVA_CORE_VERSIONS changed."
