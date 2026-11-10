#!/usr/bin/env bash
# Guided network setup; Bash 3.2+. This script does not edit .env or accounts.
set +x
set +a
set -u

usage() {
    cat <<'EOF'
Usage: ./fix-network.sh [--update]
       ./fix-network.sh --help

Guided, interactive network setup using manage.sh; no settings are changed
silently. Run from the checkout, or invoke this script by path from elsewhere.
--update first checks tracked changes and runs git pull --ff-only. Extracted
ZIPs cannot be updated this way; download a newer ZIP or use a Git checkout.
EOF
}
error() { printf 'ERROR: %s\n' "$1" >&2; }

update=0
if [ "$#" -gt 1 ]; then
    error 'Unexpected arguments; use --help for usage.'
    exit 2
fi
if [ "$#" -eq 1 ]; then
    case "$1" in
        --update) update=1 ;;
        -h|--help) usage; exit 0 ;;
        *) error 'Unknown argument; use --help for usage.'; exit 2 ;;
    esac
fi

ROOT=$(CDPATH='' cd -P "$(dirname "${BASH_SOURCE[0]}")" 2>/dev/null && pwd -P) || {
    error 'Cannot determine the checkout directory.'
    exit 1
}
validate_project() {
    if [ ! -f "$ROOT/compose.yaml" ] || [ ! -f "$ROOT/manage.sh" ] || [ ! -r "$ROOT/manage.sh" ]; then
        error 'Expected compose.yaml and a readable manage.sh next to fix-network.sh. Use the application checkout or extracted project folder.'
        return 1
    fi
}
# VERSION is public metadata. Reject symlinks, multiline and unsafe contents
# rather than accidentally printing an unrelated file or terminal controls.
version() {
    local value
    if [ -f "$ROOT/VERSION" ] && [ ! -L "$ROOT/VERSION" ] && [ -r "$ROOT/VERSION" ]; then
        value=$(cat "$ROOT/VERSION" 2>/dev/null) || value=''
        case "$value" in
            *[!0-9A-Za-z._+-]*|'') ;;
            *) case "$value" in
                   [0-9]*.*|v[0-9]*.*|V[0-9]*.*)
                       if [ "${#value}" -le 80 ]; then printf '%s' "$value"; return 0; fi ;;
               esac ;;
        esac
    fi
    printf '%s' 'unknown (VERSION missing or invalid)'
}
# Do not let inherited Git repository/index overrides redirect operations.
# Suppress Git diagnostics at call sites: remote URLs may contain credentials.
git_here() (
    unset GIT_DIR GIT_WORK_TREE GIT_INDEX_FILE GIT_COMMON_DIR GIT_OBJECT_DIRECTORY GIT_ALTERNATE_OBJECT_DIRECTORIES
    GIT_TERMINAL_PROMPT=0 git -C "$ROOT" "$@"
)

validate_project || exit 1
if ! command -v docker >/dev/null 2>&1; then
    error 'Docker is not installed or not in PATH.'
    exit 1
fi
if ! docker compose version >/dev/null 2>&1; then
    error 'Docker Compose v2 (docker compose) is required.'
    exit 1
fi
if ! docker info >/dev/null 2>&1; then
    error 'Cannot access the Docker daemon; start Docker and check your permissions.'
    exit 1
fi

if [ "$update" -eq 1 ]; then
    if ! command -v git >/dev/null 2>&1; then
        error 'Git is required for --update; for a ZIP, download a newer ZIP instead.'
        exit 1
    fi
    if [ ! -e "$ROOT/.git" ]; then
        error '--update requires a Git checkout rooted here, not an extracted ZIP. Download a newer ZIP or clone the repository.'
        exit 1
    fi
    inside=$(git_here rev-parse --is-inside-work-tree 2>/dev/null) || inside=''
    top=$(git_here rev-parse --show-toplevel 2>/dev/null) || top=''
    if [ "$inside" != true ] || [ "$top" != "$ROOT" ]; then
        error '--update requires a valid Git checkout rooted here, not an extracted ZIP inside another repository.'
        exit 1
    fi
    if ! git_here diff --quiet --ignore-submodules=none --no-ext-diff >/dev/null 2>&1 ||
       ! git_here diff --cached --quiet --ignore-submodules=none --no-ext-diff >/dev/null 2>&1; then
        error 'Tracked changes exist or could not be checked; commit or stash them before --update. No pull was attempted.'
        exit 1
    fi
    before=$(version)
    printf 'VERSION before update: %s\n' "$before"
    printf '%s\n' 'Updating with git pull --ff-only (untracked files are not checked; Git will refuse conflicting overwrites).'
    pull_ok=1
    if ! git_here pull --ff-only >/dev/null 2>&1; then pull_ok=0; fi
    after=$(version)
    printf 'VERSION after update: %s\n' "$after"
    if [ "$before" = 'unknown (VERSION missing or invalid)' ] || [ "$after" = 'unknown (VERSION missing or invalid)' ]; then
        printf '%s\n' 'VERSION change could not be determined.'
    elif [ "$before" = "$after" ]; then
        printf '%s\n' 'VERSION unchanged.'
    else
        printf '%s\n' 'VERSION changed.'
    fi
    if [ "$pull_ok" -ne 1 ]; then
        error 'Fast-forward pull failed. No network setup was started; check the branch, upstream, authentication and connection. Git details are suppressed to protect credentials.'
        exit 1
    fi
    validate_project || exit 1
else
    printf 'Current VERSION: %s\n' "$(version)"
    printf '%s\n' 'No update requested. Use --update only if this is a Git checkout and you want to update it.'
fi

cat <<'EOF'

Guided network setup: the interactive manager makes the changes, not this helper.
1. Record the current host port from docker compose ps in the project directory
   (for example, 52533 in 127.0.0.1:52533->8000/tcp). If no port is published,
   keep the prior configured port instead. Select 9 to see the configured port;
   if it differs from the current host port, select 2 and enter that host port,
   typing REUSE if prompted. Otherwise select 0 to cancel without changing it.
   Never pick a different or automatically selected port if preserving the
   current port. If this is old code or the service is not running, select 1
   (install / build / start) first, keep that configured port, and type REUSE if
   prompted. Then select 16 and create an active administrator if needed.
2. Select 15 (network access), then 2 (all IPv4 interfaces). After reviewing the
   warning, type ENABLE NETWORK to confirm, and type YES to apply now. This
   recreates the service while retaining the configured port and named data
   volume. Never choose a volume-deletion option in this flow.
   If the manager reports a port mismatch, use option 9 as described in step 1
   to retain the current host port, then retry option 15.
   If option 15 offers no apply-now prompt, this manager is old: update the
   checkout with --update (or obtain a newer ZIP), then rerun this helper.
3. After applying and exiting the manager, run in the project directory:
       docker compose ps
   Verify the actual published endpoint is 0.0.0.0:PORT->8000/tcp, rather than
   127.0.0.1:PORT->8000/tcp. Use the actual PORT and a host IP, for example
   http://<LAN-IP>:<PORT> or http://<TAILSCALE-IP>:<PORT>; 0.0.0.0 is a bind
   address, not a destination. Check connectivity from your intended client.
   Docker Desktop, WSL, remote Docker, and host forwarding can affect reachability.

SECURITY WARNING: 0.0.0.0 binds all IPv4 interfaces, possibly including public
interfaces. Verify effective host AND Docker firewall rules, and restrict access
with Tailscale ACLs as appropriate. Do not configure router port forwarding.
Plain LAN HTTP sends passwords and data in cleartext. Tailscale encrypts traffic
in transit over its tunnel, but firewall and ACL restrictions are still required.
This helper makes no firewall or Tailscale changes and stores no credentials.

Launching the interactive project manager; you remain in control of the prompts.
EOF
if (cd "$ROOT" && bash "$ROOT/manage.sh"); then
    printf '%s\n' 'Manager exited. The network change and reachability have NOT been verified; follow step 3 above.'
else
    result=$?
    error 'The manager exited with an error. The network change and reachability have NOT been verified.'
    exit "$result"
fi
