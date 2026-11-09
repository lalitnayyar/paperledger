#!/usr/bin/env bash
# Isolated deployment tests. Docker and Git are mocked; no daemon or repository is touched.
set -euo pipefail
SOURCE=$(CDPATH='' cd -P "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)
TMP=$(mktemp -d "${TMPDIR:-/tmp}/ledger-deploy-tests.XXXXXX")
trap 'rm -rf "$TMP"' EXIT
ORIG_PATH=$PATH
COUNT=0
CHECKS=0
fail() { printf 'FAIL: %s\n' "$*" >&2; exit 1; }
contains() { grep -F -q -- "$2" "$1" || fail "Expected [$2] in $1"; }
not_contains() { if grep -F -q -- "$2" "$1"; then fail "Unexpected [$2] in $1"; fi; }
check() { CHECKS=$((CHECKS + 1)); }
# Read the permission prefix only, not the filename; works with GNU and BSD ls.
# shellcheck disable=SC2012
mode() { LC_ALL=C ls -ld "$1" | cut -c1-10; }

fixture() {
  COUNT=$((COUNT + 1))
  CASE="$TMP/case-$COUNT"
  APP="$CASE/application with spaces"
  STATE="$CASE/state"
  mkdir -p "$APP" "$STATE" "$CASE/bin"
  cp "$SOURCE/deploy.sh" "$SOURCE/compose.yaml" "$APP/"
  printf '1.2.3\n' > "$APP/VERSION"
  cat > "$APP/.env.example" <<'ENV'
APP_PORT=8080
APP_BIND_ADDR=127.0.0.1
APP_ENCRYPTION_KEY=secret-from-example
ENV
  : > "$STATE/log"
  export TEST_STATE="$STATE" TEST_LOG="$STATE/log"
  export PATH="$CASE/bin:$ORIG_PATH"
  export APP_PORT=45123 APP_BIND_ADDR=0.0.0.0 APP_AUTH_ENABLED=0
  export APP_ENCRYPTION_KEY=secret-from-shell
  # These unrelated inherited variables must not be filtered by deploy.sh.
  export APP_AUTH_USER=unrelated-user APP_AUTH_HASH=unrelated-hash
  cat > "$CASE/bin/docker" <<'MOCK'
#!/usr/bin/env bash
set -u
printf 'docker:%s\n' "$*" >> "$TEST_LOG"
[ "${APP_PORT+x}" != x ] || touch "$TEST_STATE/port_leaked"
[ "${APP_BIND_ADDR+x}" != x ] || touch "$TEST_STATE/bind_leaked"
[ "${APP_AUTH_ENABLED+x}" != x ] || touch "$TEST_STATE/auth_leaked"
[ "${APP_AUTH_USER-}" = unrelated-user ] || touch "$TEST_STATE/other_env_changed"
[ "${APP_AUTH_HASH-}" = unrelated-hash ] || touch "$TEST_STATE/other_env_changed"
if [ -f "$TEST_STATE/expect_no_key" ]; then
  [ "${APP_ENCRYPTION_KEY+x}" != x ] || touch "$TEST_STATE/key_changed"
else
  [ "${APP_ENCRYPTION_KEY-}" = secret-from-shell ] || touch "$TEST_STATE/key_changed"
fi
[ "${1-}" = compose ] || exit 90
shift
if [ "${1-}" = version ]; then
  [ ! -f "$TEST_STATE/fail_version" ] || exit 1
  exit 0
fi
[ "${1-}" = --env-file ] && [ "${2-}" = .env ] || exit 91
shift 2
case "$*" in
  'up -d --build') [ ! -f "$TEST_STATE/fail_up" ] || exit 1 ;;
  'port ledger 8000')
    if [ -f "$TEST_STATE/port_output" ]; then cat "$TEST_STATE/port_output"; else printf '127.0.0.1:8080\n'; fi
    [ ! -f "$TEST_STATE/fail_port" ] || exit 1 ;;
  ps) [ ! -f "$TEST_STATE/fail_ps" ] || exit 1 ;;
  *) exit 92 ;;
esac
MOCK
  cat > "$CASE/bin/git" <<'MOCK'
#!/usr/bin/env bash
set -u
printf 'git:%s\n' "$*" >> "$TEST_LOG"
case "$*" in
  'rev-parse --is-inside-work-tree') [ ! -f "$TEST_STATE/not_checkout" ] ;;
  'diff --quiet') [ ! -f "$TEST_STATE/dirty_worktree" ] ;;
  'diff --cached --quiet') [ ! -f "$TEST_STATE/dirty_index" ] ;;
  'rev-parse --abbrev-ref @{upstream}') [ ! -f "$TEST_STATE/no_upstream" ] ;;
  'pull --ff-only')
    [ ! -f "$TEST_STATE/fail_pull" ] || exit 1
    printf '9.9.9\n' > VERSION ;;
  *) exit 93 ;;
esac
MOCK
  chmod +x "$CASE/bin/docker" "$CASE/bin/git"
}
run_ok() { (cd /; bash "$APP/deploy.sh" "$@") > "$CASE/out" 2>&1 || fail "deploy failed unexpectedly: case $COUNT"; }
run_fail() { if (cd /; bash "$APP/deploy.sh" "$@") > "$CASE/out" 2>&1; then fail "deploy should have failed: case $COUNT"; fi; }
check_env() {
  [ ! -e "$STATE/port_leaked" ] || fail 'inherited APP_PORT reached Docker Compose'
  [ ! -e "$STATE/bind_leaked" ] || fail 'inherited APP_BIND_ADDR reached Docker Compose'
  [ ! -e "$STATE/auth_leaked" ] || fail 'inherited APP_AUTH_ENABLED reached Docker Compose'
  [ ! -e "$STATE/key_changed" ] || fail 'APP_ENCRYPTION_KEY inheritance changed'
  [ ! -e "$STATE/other_env_changed" ] || fail 'unrelated shell environment changed'
  not_contains "$CASE/out" 'secret-from-shell'
  not_contains "$CASE/out" 'secret-from-example'
  not_contains "$CASE/out" 'unrelated-user'
  not_contains "$CASE/out" 'unrelated-hash'
}
assert_no_url() {
  contains "$CASE/out" 'Warning:'
  not_contains "$CASE/out" 'Open on the Docker host:'
  not_contains "$CASE/out" 'http://<LAN-IP>:'
  not_contains "$CASE/out" 'http://<TAILSCALE-IP>:'
}

bash -n "$SOURCE/deploy.sh"
bash -n "$SOURCE/tests/test_deploy.sh"
check

# Localhost startup, regardless of caller's directory or inherited shell settings.
fixture
(umask 000; run_ok)
[ -f "$APP/.env" ] || fail 'new .env missing'
[ "$(mode "$APP/.env")" = -rw------- ] || fail 'new .env is not mode 600'
cmp -s "$APP/.env" "$APP/.env.example" || fail 'example was not copied verbatim'
contains "$STATE/log" 'docker:compose --env-file .env up -d --build'
contains "$STATE/log" 'docker:compose --env-file .env port ledger 8000'
contains "$STATE/log" 'docker:compose --env-file .env ps'
not_contains "$STATE/log" 'git:'
contains "$CASE/out" 'http://127.0.0.1:8080'
not_contains "$CASE/out" 'http://<LAN-IP>:'
not_contains "$CASE/out" 'http://<TAILSCALE-IP>:'
contains "$CASE/out" 'no default credentials'
contains "$CASE/out" 'locked until an administrator is created'
contains "$CASE/out" 'bash manage.sh'
contains "$CASE/out" 'User administration'
contains "$CASE/out" 'before logging in'
contains "$CASE/out" 'Network access selection'
contains "$CASE/out" 'APP_BIND_ADDR=0.0.0.0'
contains "$CASE/out" 'LAN HTTP has no TLS'
contains "$CASE/out" 'cleartext'
contains "$CASE/out" 'do not expose this service to the public internet'
not_contains "$CASE/out" 'use HTTPS'
check_env
check

# The effective Docker output, not .env or the inherited shell, determines the URL.
fixture
printf '0.0.0.0:43210\n' > "$STATE/port_output"
run_ok
contains "$CASE/out" 'Open on the Docker host: http://127.0.0.1:43210'
contains "$CASE/out" 'http://<LAN-IP>:43210'
contains "$CASE/out" 'http://<TAILSCALE-IP>:43210'
contains "$CASE/out" 'unverified templates'
contains "$CASE/out" 'reachability is not verified'
check_env
check

fixture
printf 'APP_PORT=9999\nAPP_BIND_ADDR=0.0.0.0\nSECRET=existing-secret\n' > "$APP/.env"
chmod 640 "$APP/.env"
cp -p "$APP/.env" "$CASE/expected-env"
printf '127.0.0.1:1234\n' > "$STATE/port_output"
run_ok
cmp -s "$APP/.env" "$CASE/expected-env" || fail 'existing .env contents changed'
[ "$(mode "$APP/.env")" = -rw-r----- ] || fail 'existing .env mode changed'
contains "$CASE/out" 'http://127.0.0.1:1234'
not_contains "$CASE/out" 'http://<LAN-IP>:'
not_contains "$CASE/out" 'existing-secret'
check_env
check

# Valid boundary values, no newline, and decimal normalization.
for spec in '127.0.0.1:1|1' '127.0.0.1:65535|65535' '0.0.0.0:00008080|8080'; do
  fixture
  printf '%s' "${spec%%|*}" > "$STATE/port_output"
  run_ok
  contains "$CASE/out" "http://127.0.0.1:${spec##*|}"
  check
 done

# Malformed, binary, multiple, unsupported, or out-of-range output is not displayed.
for spec in '' '127.0.0.1:0\n' '0.0.0.0:65536\n' '127.0.0.1:999999999999999999999\n' \
  '127.0.0.1:-1\n' '127.0.0.1:+80\n' '127.0.0.1:abc\n' '127.0.0.1:80\r\n' \
  '[::]:8080\n' '10.0.0.1:8080\n' '127.0.0.1:80\n0.0.0.0:80\n' \
  '127.0.0.1:80\n\n' ' 127.0.0.1:80\n' '127.0.0.1:80\000\n' \
  '127.0.0.1:80\nprivate-unrecognized-output\n'; do
  fixture
  printf '%b' "$spec" > "$STATE/port_output"
  run_ok
  assert_no_url
  not_contains "$CASE/out" 'private-unrecognized-output'
  check
 done
fixture
printf '127.0.0.1:9999\n' > "$STATE/port_output"
touch "$STATE/fail_port"
run_ok
assert_no_url
check

# Never follow existing or dangling symlinks, and reject other non-regular files.
fixture
printf 'protected-target\n' > "$CASE/target"
ln -s "$CASE/target" "$APP/.env"
run_fail
contains "$CASE/out" 'Refusing to use .env'
[ "$(cat "$CASE/target")" = protected-target ] || fail 'symlink target changed'
not_contains "$STATE/log" ' up '
check
fixture
ln -s "$CASE/missing-target" "$APP/.env"
run_fail
contains "$CASE/out" 'Refusing to use .env'
[ ! -e "$CASE/missing-target" ] || fail 'dangling symlink target was created'
not_contains "$STATE/log" ' up '
check
for kind in directory fifo; do
  fixture
  if [ "$kind" = directory ]; then mkdir "$APP/.env"; else mkfifo "$APP/.env"; fi
  run_fail
  contains "$CASE/out" 'Refusing to use .env'
  not_contains "$STATE/log" ' up '
  check
 done
fixture
rm "$APP/.env.example"
run_fail
contains "$CASE/out" 'Cannot create .env'
[ ! -e "$APP/.env" ] || fail '.env created without template'
not_contains "$STATE/log" ' up '
check

# Inheriting no encryption key must also remain unchanged.
fixture
unset APP_ENCRYPTION_KEY
touch "$STATE/expect_no_key"
run_ok
check_env
check

# Failed builds do not attempt port reporting; Compose version is checked first.
fixture
touch "$STATE/fail_up"
run_fail
not_contains "$STATE/log" ' port ledger 8000'
not_contains "$STATE/log" ' --env-file .env ps'
check
fixture
touch "$STATE/fail_version"
run_fail
contains "$CASE/out" 'Docker Compose v2 is required'
[ ! -e "$APP/.env" ] || fail '.env created despite failed version check'
check

# Simulate Git preconditions and pull; never invoke real Git or change this checkout.
fixture
run_ok --update
contains "$STATE/log" 'git:rev-parse --is-inside-work-tree'
contains "$STATE/log" 'git:diff --quiet'
contains "$STATE/log" 'git:diff --cached --quiet'
contains "$STATE/log" 'git:rev-parse --abbrev-ref @{upstream}'
contains "$STATE/log" 'git:pull --ff-only'
contains "$CASE/out" 'Current application version: 1.2.3'
contains "$CASE/out" 'Application version after pull: 9.9.9'
contains "$STATE/log" 'docker:compose --env-file .env up -d --build'
check_env
check
fixture
printf 'APP_PORT=2345\nSECRET=existing-update-secret\n' > "$APP/.env"
chmod 600 "$APP/.env"
cp -p "$APP/.env" "$CASE/expected-env"
run_ok --update
cmp -s "$APP/.env" "$CASE/expected-env" || fail 'update changed existing .env'
[ "$(mode "$APP/.env")" = -rw------- ] || fail 'update changed existing .env mode'
not_contains "$CASE/out" 'existing-update-secret'
check
for failure in not_checkout dirty_worktree dirty_index no_upstream fail_pull; do
  fixture
  touch "$STATE/$failure"
  run_fail --update
  not_contains "$STATE/log" 'docker:compose --env-file .env up -d --build'
  if [ "$failure" != fail_pull ]; then not_contains "$STATE/log" 'git:pull --ff-only'; fi
  check
 done

fixture
run_ok --help
contains "$CASE/out" 'Usage: ./deploy.sh [--update]'
[ ! -s "$STATE/log" ] || fail 'help invoked Docker or Git'
run_fail --bogus
contains "$CASE/out" 'Unknown argument'
[ ! -s "$STATE/log" ] || fail 'invalid argument invoked Docker or Git'
run_fail 'password=do-not-echo-this'
not_contains "$CASE/out" 'do-not-echo-this'
check

printf 'PASS: %s deploy test groups (mock Docker/Git; no real Docker or Git used)\n' "$CHECKS"
