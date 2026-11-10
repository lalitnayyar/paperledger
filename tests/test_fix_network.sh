#!/usr/bin/env bash
# Hermetic network-helper tests: never invoke real Docker, Git, or manage.sh.
set -euo pipefail
SOURCE=$(CDPATH='' cd -P "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)
TMP=$(mktemp -d "${TMPDIR:-/tmp}/ledger-network-tests.XXXXXX")
trap 'rm -rf "$TMP"' EXIT
ORIGINAL_PATH=$PATH
COUNT=0
CHECKS=0
fail() { printf 'FAIL: %s\n' "$*" >&2; exit 1; }
contains() { grep -F -q -- "$2" "$1" || fail "Expected [$2] in $1"; }
not_contains() { if grep -F -q -- "$2" "$1"; then fail "Unexpected [$2] in $1"; fi; }
check() { CHECKS=$((CHECKS + 1)); }

fixture() {
    COUNT=$((COUNT + 1))
    CASE="$TMP/case-$COUNT"
    APP="$CASE/project with spaces"
    STATE="$CASE/state"
    mkdir -p "$APP/.git" "$STATE" "$CASE/bin" "$CASE/elsewhere"
    cp "$SOURCE/fix-network.sh" "$APP/fix-network.sh"
    printf 'services: {}\n' > "$APP/compose.yaml"
    printf '1.5.0\n' > "$APP/VERSION"
    cat > "$APP/.env" <<'ENV'
APP_PORT=52533
APP_BIND_ADDR=127.0.0.1
APP_ENCRYPTION_KEY=secret-from-env-DO-NOT-PRINT
DANGEROUS=$(touch "$TEST_STATE/was-sourced")
ENV
    cp "$APP/.env" "$CASE/expected-env"
    printf '15\n2\nENABLE NETWORK\nYES\n0\n' > "$CASE/input"
    : > "$STATE/log"
    export TEST_STATE="$STATE" TEST_LOG="$STATE/log" TEST_APP="$APP"
    export PATH="$CASE/bin:$ORIGINAL_PATH"
    cat > "$APP/manage.sh" <<'MANAGER'
#!/usr/bin/env bash
printf '%s\n' "$PWD" > "$TEST_STATE/manager-pwd"
printf '%s\n' "$#" > "$TEST_STATE/manager-argc"
cat > "$TEST_STATE/manager-stdin"
printf '%s\n' 'MOCK MANAGER RAN'
[ ! -f "$TEST_STATE/manager-fails" ] || exit 23
MANAGER
    cat > "$CASE/bin/docker" <<'DOCKER'
#!/usr/bin/env bash
printf 'docker:%s\n' "$*" >> "$TEST_LOG"
printf '%s\n' 'secret-from-docker-DO-NOT-PRINT' >&2
case "$*" in
    'compose version') [ ! -f "$TEST_STATE/compose-fails" ] ;;
    'info') [ ! -f "$TEST_STATE/daemon-fails" ] ;;
    *) printf '%s\n' 'unexpected Docker operation' >&2; exit 99 ;;
esac
DOCKER
    cat > "$CASE/bin/git" <<'GIT'
#!/usr/bin/env bash
printf 'git:%s\n' "$*" >> "$TEST_LOG"
# Any leak of Git repository/index overrides could redirect operations.
for name in GIT_DIR GIT_WORK_TREE GIT_INDEX_FILE GIT_COMMON_DIR GIT_OBJECT_DIRECTORY GIT_ALTERNATE_OBJECT_DIRECTORIES; do
    eval "present=\${$name+x}"
    [ "$present" != x ] || { touch "$TEST_STATE/override-leaked"; exit 98; }
done
[ "${GIT_TERMINAL_PROMPT-}" = 0 ] || { touch "$TEST_STATE/prompt-not-disabled"; exit 97; }
[ "${1-}" = -C ] && [ "${2-}" = "$TEST_APP" ] || exit 96
shift 2
case "$*" in
    'rev-parse --is-inside-work-tree')
        [ ! -f "$TEST_STATE/not-repo" ] || exit 1
        printf 'true\n' ;;
    'rev-parse --show-toplevel')
        if [ -f "$TEST_STATE/wrong-root" ]; then printf '/unrelated\n'; else printf '%s\n' "$TEST_APP"; fi ;;
    'diff --quiet --ignore-submodules=none --no-ext-diff')
        [ ! -f "$TEST_STATE/dirty-worktree" ] ;;
    'diff --cached --quiet --ignore-submodules=none --no-ext-diff')
        [ ! -f "$TEST_STATE/dirty-index" ] ;;
    'pull --ff-only')
        printf '%s\n' 'secret-from-git-DO-NOT-PRINT' 'https://user:credential@example.invalid/'
        printf '%s\n' 'secret-from-git-error-DO-NOT-PRINT' >&2
        [ ! -f "$TEST_STATE/pull-fails" ] || exit 1
        if [ -f "$TEST_STATE/new-version" ]; then cat "$TEST_STATE/new-version" > "$TEST_APP/VERSION"; fi
        if [ -f "$TEST_STATE/remove-compose" ]; then rm "$TEST_APP/compose.yaml"; fi ;;
    *) printf 'unexpected git operation\n' >&2; exit 95 ;;
esac
GIT
    chmod +x "$CASE/bin/docker" "$CASE/bin/git"
}
run_ok() {
    (cd "$CASE/elsewhere"; "$BASH" "$APP/fix-network.sh" "$@" < "$CASE/input") > "$CASE/out" 2>&1 || fail "Helper unexpectedly failed in case $COUNT"
}
run_fail() {
    if (cd "$CASE/elsewhere"; "$BASH" "$APP/fix-network.sh" "$@" < "$CASE/input") > "$CASE/out" 2>&1; then
        fail "Helper unexpectedly succeeded in case $COUNT"
    fi
}
no_manager() { [ ! -e "$STATE/manager-pwd" ] || fail 'Manager ran despite earlier failure'; }
check_secret_safety() {
    cmp -s "$APP/.env" "$CASE/expected-env" || fail '.env was edited'
    [ ! -e "$STATE/was-sourced" ] || fail '.env was evaluated'
    not_contains "$CASE/out" 'secret-from-env-DO-NOT-PRINT'
    not_contains "$CASE/out" 'secret-from-docker-DO-NOT-PRINT'
    not_contains "$CASE/out" 'secret-from-git-DO-NOT-PRINT'
    not_contains "$CASE/out" 'secret-from-git-error-DO-NOT-PRINT'
    not_contains "$CASE/out" 'user:credential@example.invalid'
}
assert_only_docker_checks() {
    [ "$(grep -c '^docker:' "$STATE/log" || :)" -eq 2 ] || fail 'Unexpected Docker operation count'
    contains "$STATE/log" 'docker:compose version'
    contains "$STATE/log" 'docker:info'
}

bash -n "$SOURCE/fix-network.sh"
bash -n "$SOURCE/tests/test_fix_network.sh"
check

# Default mode is an interactive manager launcher, with no Git or data changes.
fixture
run_ok
contains "$CASE/out" 'Current VERSION: 1.5.0'
contains "$CASE/out" 'No update requested'
contains "$CASE/out" 'Guided network setup'
contains "$CASE/out" 'select 1'
contains "$CASE/out" '(install / build / start)'
contains "$CASE/out" 'select 16'
contains "$CASE/out" 'Select 9 to see the configured port'
contains "$CASE/out" 'select 2 and enter that host port'
contains "$CASE/out" 'REUSE if prompted'
contains "$CASE/out" 'Select 15'
contains "$CASE/out" 'then 2'
contains "$CASE/out" 'ENABLE NETWORK'
contains "$CASE/out" 'type YES to apply now'
contains "$CASE/out" 'configured port and named data'
contains "$CASE/out" 'docker compose ps'
contains "$CASE/out" '0.0.0.0:PORT->8000/tcp'
contains "$CASE/out" '127.0.0.1:PORT->8000/tcp'
contains "$CASE/out" 'not a destination'
contains "$CASE/out" 'possibly including public'
contains "$CASE/out" 'host AND Docker firewall'
contains "$CASE/out" 'Tailscale ACLs'
contains "$CASE/out" 'Do not configure router port forwarding'
contains "$CASE/out" 'passwords and data in cleartext'
contains "$CASE/out" 'Tailscale encrypts traffic'
contains "$CASE/out" 'firewall and ACL restrictions are still required'
contains "$CASE/out" 'manager is old'
contains "$CASE/out" 'MOCK MANAGER RAN'
contains "$CASE/out" 'NOT been verified'
not_contains "$CASE/out" 'Network change succeeded'
not_contains "$STATE/log" 'git:'
assert_only_docker_checks
[ "$(cat "$STATE/manager-pwd")" = "$APP" ] || fail 'Manager did not run in checkout root'
[ "$(cat "$STATE/manager-argc")" = 0 ] || fail 'Unexpected manager arguments'
cmp -s "$STATE/manager-stdin" "$CASE/input" || fail 'Interactive input was not passed unchanged'
check_secret_safety
check

# The helper is a child process and must not change the caller's working directory.
fixture
(cd "$CASE/elsewhere"; before=$PWD; "$BASH" "$APP/fix-network.sh" < "$CASE/input" > "$CASE/out" 2>&1; [ "$PWD" = "$before" ]) || fail 'Caller directory changed'
check

# Help is dependency-independent and does not launch anything.
fixture
rm "$APP/compose.yaml" "$APP/manage.sh"
run_ok --help
contains "$CASE/out" 'Usage: ./fix-network.sh [--update]'
contains "$CASE/out" 'Usage:'
[ ! -s "$STATE/log" ] || fail 'Help invoked Docker or Git'
no_manager
check
fixture
run_ok -h
contains "$CASE/out" 'Usage:'
[ ! -s "$STATE/log" ] || fail '-h invoked Docker or Git'
check

# Unknown, positional, duplicate and combined options are rejected without
# printing the supplied argument (which might be a secret).
for arg in unknown --bad --update=1 secret-argument-DO-NOT-PRINT; do
    fixture
    run_fail "$arg"
    contains "$CASE/out" 'Unknown argument'
    not_contains "$CASE/out" 'secret-argument-DO-NOT-PRINT'
    [ ! -s "$STATE/log" ] || fail 'Invalid argument invoked Docker or Git'
    no_manager
    check
done
fixture
run_fail --update --help
contains "$CASE/out" 'Unexpected arguments'
[ ! -s "$STATE/log" ] || fail 'Extra arguments invoked Docker or Git'
check

# Validate the project before checking dependencies or starting the manager.
for missing in compose.yaml manage.sh; do
    fixture
    rm "$APP/$missing"
    run_fail
    contains "$CASE/out" 'Expected compose.yaml'
    [ ! -s "$STATE/log" ] || fail 'Missing project file invoked Docker or Git'
    no_manager
    check
done

# Docker/Compose checks cannot silently continue on a failure.
fixture
touch "$STATE/compose-fails"
run_fail
contains "$CASE/out" 'Docker Compose v2'
not_contains "$STATE/log" 'docker:info'
no_manager
check_secret_safety
check
fixture
touch "$STATE/daemon-fails"
run_fail
contains "$CASE/out" 'Cannot access the Docker daemon'
no_manager
check_secret_safety
check
fixture
rm "$CASE/bin/docker"
ln -s "$(command -v dirname)" "$CASE/bin/dirname"
PATH="$CASE/bin" run_fail
contains "$CASE/out" 'Docker is not installed'
no_manager
check

# A valid clean checkout uses only this project and exactly pull --ff-only.
fixture
printf '1.6.0\n' > "$STATE/new-version"
export GIT_DIR=/not-this-repo GIT_WORK_TREE=/not-this-worktree GIT_INDEX_FILE=/not-this-index
export GIT_COMMON_DIR=/not-this-common GIT_OBJECT_DIRECTORY=/not-this-objects GIT_ALTERNATE_OBJECT_DIRECTORIES=/not-these-objects
run_ok --update
unset GIT_DIR GIT_WORK_TREE GIT_INDEX_FILE GIT_COMMON_DIR GIT_OBJECT_DIRECTORY GIT_ALTERNATE_OBJECT_DIRECTORIES
contains "$CASE/out" 'VERSION before update: 1.5.0'
contains "$CASE/out" 'VERSION after update: 1.6.0'
contains "$CASE/out" 'VERSION changed.'
contains "$CASE/out" 'untracked files are not checked'
contains "$STATE/log" "git:-C $APP diff --quiet --ignore-submodules=none --no-ext-diff"
contains "$STATE/log" "git:-C $APP diff --cached --quiet --ignore-submodules=none --no-ext-diff"
contains "$STATE/log" "git:-C $APP pull --ff-only"
[ "$(grep -c 'pull --ff-only' "$STATE/log")" -eq 1 ] || fail 'Expected exactly one fast-forward pull'
[ ! -e "$STATE/override-leaked" ] || fail 'Git repository override leaked'
[ ! -e "$STATE/prompt-not-disabled" ] || fail 'Git terminal credential prompt enabled'
[ "$(cat "$STATE/manager-pwd")" = "$APP" ] || fail 'Manager not launched at root after update'
assert_only_docker_checks
check_secret_safety
check

# Worktrees can use a .git file, and unchanged VERSION does not imply a new commit.
fixture
rmdir "$APP/.git"
printf 'gitdir: somewhere\n' > "$APP/.git"
run_ok --update
contains "$CASE/out" 'VERSION before update: 1.5.0'
contains "$CASE/out" 'VERSION after update: 1.5.0'
contains "$CASE/out" 'VERSION unchanged.'
check

# An unreadable or invalid version cannot establish equality or a change.
fixture
rm "$APP/VERSION"
run_ok --update
contains "$CASE/out" 'VERSION before update: unknown (VERSION missing or invalid)'
contains "$CASE/out" 'VERSION after update: unknown (VERSION missing or invalid)'
contains "$CASE/out" 'VERSION change could not be determined.'
not_contains "$CASE/out" 'VERSION unchanged.'
check

# Non-Git ZIPs, invalid/nested checkouts, and dirty tracked files never pull.
fixture
rm -rf "$APP/.git"
run_fail --update
contains "$CASE/out" 'not an extracted ZIP'
not_contains "$STATE/log" 'git:'
no_manager
check
for state in not-repo wrong-root dirty-worktree dirty-index; do
    fixture
    touch "$STATE/$state"
    run_fail --update
    case "$state" in
        dirty-*) contains "$CASE/out" 'Tracked changes exist or could not be checked' ;;
        *) contains "$CASE/out" 'valid Git checkout rooted here' ;;
    esac
    not_contains "$STATE/log" 'pull --ff-only'
    no_manager
    check_secret_safety
    check
done

# Pull failure reports both versions, hides Git's output, and never launches.
fixture
touch "$STATE/pull-fails"
run_fail --update
contains "$CASE/out" 'VERSION before update: 1.5.0'
contains "$CASE/out" 'VERSION after update: 1.5.0'
contains "$CASE/out" 'Fast-forward pull failed'
not_contains "$CASE/out" 'MOCK MANAGER RAN'
no_manager
check_secret_safety
check

# Validate project files again after the pull before running the new manager.
fixture
touch "$STATE/remove-compose"
run_fail --update
contains "$CASE/out" 'Expected compose.yaml'
no_manager
check

# Git missing: a restricted PATH contains the stubs and utilities, not real Git.
fixture
rm "$CASE/bin/git"
ln -s "$(command -v dirname)" "$CASE/bin/dirname"
ln -s "$(command -v bash)" "$CASE/bin/bash"
PATH="$CASE/bin" run_fail --update
contains "$CASE/out" 'Git is required for --update'
no_manager
check

# Invalid/symlinked VERSION cannot print arbitrary file contents or control text.
fixture
rm "$APP/VERSION"
ln -s "$APP/.env" "$APP/VERSION"
run_ok
contains "$CASE/out" 'Current VERSION: unknown (VERSION missing or invalid)'
check_secret_safety
check
fixture
printf '1.5.0\nsecret-version-line\n' > "$APP/VERSION"
run_ok
contains "$CASE/out" 'Current VERSION: unknown (VERSION missing or invalid)'
not_contains "$CASE/out" 'secret-version-line'
check

# A manager failure is propagated, and a normal manager exit is not treated as
# proof that any setting was applied or any host can connect.
fixture
touch "$STATE/manager-fails"
set +e
(cd "$CASE/elsewhere"; bash "$APP/fix-network.sh" < "$CASE/input") > "$CASE/out" 2>&1
rc=$?
set -e
[ "$rc" -eq 23 ] || fail "Manager exit status not preserved: $rc"
contains "$CASE/out" 'manager exited with an error'
contains "$CASE/out" 'NOT been verified'
not_contains "$CASE/out" 'Manager exited. The network change'
check_secret_safety
check

printf 'PASS: %s checks (%s isolated cases); no real Docker or Git used.\n' "$CHECKS" "$COUNT"
