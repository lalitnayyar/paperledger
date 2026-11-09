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
    *) echo 'Unknown argument.' >&2; usage; exit 2 ;;
  esac
done

# Compose gives the shell precedence over .env. Do not let an inherited shell
# setting silently change the port, network exposure, or login configuration.
# APP_ENCRYPTION_KEY deliberately retains its existing shell precedence.
unset APP_PORT APP_BIND_ADDR APP_AUTH_ENABLED

command -v docker >/dev/null || { echo 'Docker is required.' >&2; exit 1; }
docker compose version >/dev/null 2>&1 || { echo 'Docker Compose v2 is required.' >&2; exit 1; }
version_before=$(cat VERSION)
echo "Current application version: $version_before"
if [ "$UPDATE" -eq 1 ]; then
  git rev-parse --is-inside-work-tree >/dev/null 2>&1 || { echo 'Update requires a Git checkout, not an extracted ZIP.' >&2; exit 1; }
  if ! git diff --quiet || ! git diff --cached --quiet; then
    echo 'Commit or stash tracked local changes before updating.' >&2
    exit 1
  fi
  git rev-parse --abbrev-ref '@{upstream}' >/dev/null 2>&1 || { echo 'Configure a Git upstream first.' >&2; exit 1; }
  git pull --ff-only
  echo "Application version after pull: $(cat VERSION)"
fi

# Never follow an existing symlink (including a dangling one), or accept a
# directory, device, FIFO, etc. Leave an existing regular file untouched.
if [ -L .env ] || { [ -e .env ] && [ ! -f .env ]; }; then
  echo 'Refusing to use .env: it must be a regular file, not a symlink or special file.' >&2
  exit 1
fi
if [ ! -e .env ]; then
  if [ ! -f .env.example ] || [ ! -r .env.example ]; then
    echo 'Cannot create .env: .env.example is not a readable regular file.' >&2
    exit 1
  fi
  # Open the template first, then create .env with noclobber and restrictive
  # permissions. A concurrent creator cannot have its regular file overwritten.
  if (umask 077; set -o noclobber; cat < .env.example > .env); then
    if [ -L .env ] || [ ! -f .env ]; then
      echo 'Refusing to use .env: it is not a regular file.' >&2
      exit 1
    fi
    chmod 600 .env
    echo 'Created .env from .env.example with permissions 600.'
  else
    echo 'Could not safely create .env; check for an existing file or incomplete copy.' >&2
    exit 1
  fi
fi

# Explicitly select this file even if Compose's default env-file settings have
# been changed. This does not source or evaluate it as shell code.
compose() {
  docker compose --env-file .env "$@"
}
compose up -d --build

# Retain the exact line endings by appending a sentinel inside the command
# substitution. Accept only one IPv4 address/port line (with at most one final
# newline). Bash command substitution drops NULs, so detect them first. Never
# echo unrecognized output or connect to an advertised address.
if endpoint=$(compose port ledger 8000 2>/dev/null | (
  if IFS= read -r -d '' raw_endpoint; then exit 1; fi
  printf '%s.' "$raw_endpoint"
)); then
  endpoint=${endpoint%.}
  endpoint=${endpoint%$'\n'}
  valid_endpoint=0
  if [ "${#endpoint}" -le 256 ] && [[ $endpoint =~ ^(127[.]0[.]0[.]1|0[.]0[.]0[.]0):([0-9]+)$ ]]; then
    bind_addr=${BASH_REMATCH[1]}
    port=${BASH_REMATCH[2]}
    # Strip leading zeroes without arithmetic overflow or octal interpretation.
    port=${port#"${port%%[!0]*}"}
    if [ -n "$port" ] && [ "${#port}" -le 5 ] && [ "$port" -ge 1 ] && [ "$port" -le 65535 ]; then
      valid_endpoint=1
    fi
  fi
  if [ "$valid_endpoint" -eq 1 ]; then
    echo "Open on the Docker host: http://127.0.0.1:$port"
    if [ "$bind_addr" = '0.0.0.0' ]; then
      echo 'The service is bound to all IPv4 interfaces. The following are unverified templates:'
      echo "  LAN:       http://<LAN-IP>:$port"
      echo "  Tailscale: http://<TAILSCALE-IP>:$port"
      echo 'Replace the placeholders with the host addresses; reachability is not verified.'
    fi
  else
    echo 'Warning: could not determine a single supported IPv4 endpoint from Docker Compose; inspect the port mapping manually.' >&2
  fi
else
  echo 'Warning: Docker Compose could not report a valid ledger port; inspect the port mapping manually.' >&2
fi

echo 'Authentication is enabled. A new installation has no default credentials and is locked until an administrator is created.'
echo 'After startup, run bash manage.sh and use User administration to create the first administrator before logging in.'
echo 'Existing users and credentials are stored in the persistent data volume.'
echo 'For LAN or Tailscale access, after creating an administrator, choose Network access selection in bash manage.sh, then rerun bash deploy.sh.'
echo 'That setting uses APP_BIND_ADDR=0.0.0.0, which binds all IPv4 interfaces.'
echo 'Warning: LAN HTTP has no TLS; passwords and application data are sent in cleartext on the LAN.'
echo 'Use only trusted private networks and Tailscale (which encrypts traffic between tailnet devices); do not expose this service to the public internet.'
echo 'Verify effective Docker and host firewall rules and Tailscale policies; remote reachability is not verified.'
compose ps
