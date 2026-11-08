#!/usr/bin/env bash
# Personal project; educational use only. No investment advice or profit guarantees.
set -euo pipefail
cd "$(dirname "$0")"
usage() {
  cat <<'EOF'
Usage: ./deploy.sh [--update]

Build and start Ledger Six using Docker Compose. Existing data is preserved.
--update   Pull the current Git branch using fast-forward only before rebuilding.
           Requires an existing Git checkout with a configured upstream.
EOF
}
UPDATE=0
for arg in "$@"; do
  case "$arg" in
    --update) UPDATE=1 ;;
    -h|--help) usage; exit 0 ;;
    *) echo "Unknown argument: $arg" >&2; usage; exit 2 ;;
  esac
done
command -v docker >/dev/null || { echo 'Docker is required.' >&2; exit 1; }
docker compose version >/dev/null 2>&1 || { echo 'Docker Compose v2 is required.' >&2; exit 1; }
version_before=$(cat VERSION)
echo "Current application version: $version_before"
if [ "$UPDATE" -eq 1 ]; then
  git rev-parse --is-inside-work-tree >/dev/null 2>&1 || { echo 'Update requires a Git checkout, not an extracted ZIP.' >&2; exit 1; }
  git diff --quiet && git diff --cached --quiet || { echo 'Commit or stash tracked local changes before updating.' >&2; exit 1; }
  git rev-parse --abbrev-ref '@{upstream}' >/dev/null 2>&1 || { echo 'Configure a Git upstream first.' >&2; exit 1; }
  git pull --ff-only
  echo "Application version after pull: $(cat VERSION)"
fi
if [ ! -f .env ]; then cp .env.example .env; echo 'Created .env from .env.example.'; fi
docker compose up -d --build
PORT=$(grep '^APP_PORT=' .env | tail -1 | cut -d= -f2- || true)
echo "Open http://127.0.0.1:${PORT:-8080}"
docker compose ps
