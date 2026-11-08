#!/usr/bin/env bash
# Isolated integration tests; Docker is simulated and no real volumes are touched.
# Each A && B || fail assertion intentionally fails if either check fails.
# shellcheck disable=SC2015
set -euo pipefail
SOURCE=$(CDPATH='' cd -P "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)
TMP=$(mktemp -d "${TMPDIR:-/tmp}/ledger-manage-tests.XXXXXX")
PORT_HOLDERS=
trap 'for pid in $PORT_HOLDERS; do kill "$pid" 2>/dev/null || :; done; rm -rf "$TMP"' EXIT
ORIG_PATH=$PATH
SOURCE_VERSION=$(cat "$SOURCE/VERSION")
COUNT=0
fail() { printf 'FAIL: %s\n' "$*" >&2; exit 1; }
contains() { grep -F -q -- "$2" "$1" || fail "Expected [$2] in $1"; }
not_contains() { if grep -F -q -- "$2" "$1"; then fail "Unexpected [$2] in $1"; fi; }
# Only the fixed permission prefix is parsed, not the filename.
# shellcheck disable=SC2012
is_private_file() { [ "$(LC_ALL=C ls -ld "$1" | cut -c1-10)" = '-rw-------' ]; }
fixture() {
    COUNT=$((COUNT+1))
    CASE="$TMP/case-$COUNT"
    APP="$CASE/application with spaces"
    STATE="$CASE/state"
    mkdir -p "$APP" "$STATE/data" "$CASE/bin"
    cp "$SOURCE/manage.sh" "$SOURCE/compose.yaml" "$SOURCE/.env.example" "$SOURCE/VERSION" "$APP/"
    printf 'original ledger\n' > "$STATE/data/ledger.txt"
    touch "$STATE/container" "$STATE/running" "$STATE/volume" "$STATE/image"
    printf '8080\n' > "$STATE/port"
    : > "$STATE/log"
    export TEST_STATE="$STATE" TEST_LOG="$STATE/log"
    export PATH="$CASE/bin:$ORIG_PATH"
    cat > "$CASE/bin/docker" <<'MOCK'
#!/usr/bin/env bash
set -u
s=$TEST_STATE
{ for arg in "$@"; do printf '%s\t' "$arg"; done; printf '\n'; } >> "$TEST_LOG"
cmd=${1-}; [ "$#" -gt 0 ] && shift
case "$cmd" in
 info) exit 0 ;;
 compose)
    if [ "${1-}" = version ]; then exit 0; fi
    [ -z "${APP_PORT+x}" ] || touch "$s/shell_port_leaked"
    env_file=
    while [ "$#" -gt 0 ]; do
        case "$1" in --env-file) env_file=$2; shift 2 ;; --project-directory|-f) shift 2 ;; *) break ;; esac
    done
    sub=${1-}; [ "$#" -gt 0 ] && shift
    case "$sub" in
        ps) for x in "$@"; do if [ "$x" = --quiet ]; then
                [ ! -f "$s/container" ] || printf '%s\n' aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa
                [ ! -f "$s/second" ] || printf '%s\n' bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb
                exit 0
            fi; done; exit 0 ;;
        up) [ ! -f "$s/fail_up" ] || { printf 'simulated port conflict\n' >&2; exit 1; }
            if [ -n "$env_file" ]; then
                p=$(awk -F= '/^APP_PORT=[0-9]+$/ { p=$2 } END { print p }' "$env_file")
                [ -z "$p" ] || printf '%s\n' "$p" > "$s/port"
            fi
            touch "$s/container" "$s/running" "$s/volume" "$s/image" ;;
        port) [ ! -f "$s/fail_port" ] || exit 1
              if [ -f "$s/reported_port" ]; then cat "$s/reported_port"; else printf '127.0.0.1:%s\n' "$(cat "$s/port")"; fi ;;
        down) rm -f "$s/container" "$s/running" ;;
        start) touch "$s/running" ;;
        stop) rm -f "$s/running" ;;
        restart|logs) : ;;
        *) printf 'unexpected compose command: %s\n' "$sub" >&2; exit 2 ;;
    esac ;;
 inspect)
    [ -f "$s/container" ] || exit 1
    running=false; [ -f "$s/running" ] && running=true
    case "$*" in *'{{.Id}} {{.State.Running}}'*) printf '%s %s\n' aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa "$running" ;; *) printf '%s\n' "$running" ;; esac ;;
 ps)
    all=0; filter=
    while [ "$#" -gt 0 ]; do
        case "$1" in --all) all=1; shift ;; --filter) filter=$2; shift 2 ;; *) shift ;; esac
    done
    if [ "$filter" = volume=ledger-six-data ] && [ -f "$s/container" ] && { [ -f "$s/running" ] || [ "$all" = 1 ]; }; then
        printf '%s\n' aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa
    fi
    if [ -f "$s/other" ] && { [ "$all" = 1 ] || [ -f "$s/other_running" ]; }; then
        printf '%s\n' bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb
    fi ;;
 stop) rm -f "$s/running" ;;
 start) [ -f "$s/fail_start" ] && exit 1; touch "$s/running" ;;
 logs) id=${2-}; printf 'stdout for %s: sensitive-test-log\n' "$id"; printf 'stderr for %s\n' "$id" >&2
       [ ! -f "$s/fail_logs" ] || exit 1 ;;
 volume)
    case "${1-}" in
      ls) [ -f "$s/fail_volume_ls" ] && exit 1; [ -f "$s/volume" ] && printf 'ledger-six-data\n'; exit 0 ;;
      create) touch "$s/volume" ;;
      rm) rm -f "$s/volume" ;;
      *) exit 2 ;;
    esac ;;
 image)
    case "${1-}" in
      ls) [ -f "$s/image" ] && printf 'ledger-six:local\n'; exit 0 ;;
      rm) rm -f "$s/image" ;;
      *) exit 2 ;;
    esac ;;
 run)
    while [ "$#" -gt 0 ] && [ "$1" != alpine:3.20 ]; do shift; done
    [ "$#" -gt 0 ] || exit 2; shift
    if [ "${1-}" = tar ] && [ "${2-}" = czf ]; then
        [ -f "$s/slow_archive" ] && sleep 2
        [ -f "$s/fail_archive" ] && exit 1
        tar czf - -C "$s/data" .
    elif [ "${1-}" = tar ] && [ "${2-}" = tzf ]; then
        tar tzf -
    elif [ "${1-}" = sh ]; then
        # Simulate the volume-only clear/extract/chown, not the host filesystem.
        find "$s/data" -mindepth 1 -maxdepth 1 -exec rm -rf -- {} \;
        [ -f "$s/fail_restore" ] && exit 1
        tar xzf - -C "$s/data"
    else printf '%s\n' 'Unexpected helper command' >&2; exit 2; fi ;;
 *) printf 'unexpected docker command: %s\n' "$cmd" >&2; exit 2 ;;
esac
MOCK
    chmod +x "$CASE/bin/docker"
}
run_menu() { printf '%b' "$1" | bash "$APP/manage.sh" > "$CASE/out" 2>&1; }
bash -n "$SOURCE/manage.sh"
fixture
(cd /; printf '1\n1\n0\n' | bash "$APP/manage.sh" > "$CASE/out" 2>&1)
[ -f "$APP/.env" ] || fail 'deploy did not create .env'
is_private_file "$APP/.env" || fail '.env permissions were not 600'
contains "$STATE/log" $'up\t-d\t--build\tledger'
contains "$CASE/out" "Current VERSION: $SOURCE_VERSION"

fixture
cat > "$APP/.env" <<'ENV'
# preserved comment
APP_PORT=8080
SECRET_KEY=never-print-this-secret
OTHER='line one
APP_PORT=leave-this-alone
line three'
export APP_PORT = '8081'
APP_PORT: 8082
APP_PORT
ENV
run_menu '9\n2\n009999\n0\n'
contains "$APP/.env" 'APP_PORT=9999'
contains "$APP/.env" 'APP_PORT=leave-this-alone'
contains "$APP/.env" 'SECRET_KEY=never-print-this-secret'
not_contains "$CASE/out" 'never-print-this-secret'
is_private_file "$APP/.env" || fail 'edited .env permissions were not 600'
[ "$(grep -c '^APP_PORT=9999$' "$APP/.env")" = 4 ] || fail 'duplicate APP_PORT entries not replaced'
cp "$APP/.env" "$CASE/before"
run_menu '9\n2\n65536\n0\n'
cmp -s "$CASE/before" "$APP/.env" || fail 'invalid port modified .env'

fixture
printf "SECRET='unclosed\ncontinued\n" > "$APP/.env"
cp "$APP/.env" "$CASE/before"
run_menu '9\n2\n8081\n0\n'
cmp -s "$CASE/before" "$APP/.env" || fail 'malformed multiline variable was altered'
contains "$CASE/out" 'Could not safely update .env'

fixture
run_menu '2\n0\n'
contains "$CASE/out" 'not an extracted ZIP'
not_contains "$STATE/log" $'up\t-d'

fixture
rm -f "$STATE/volume"
run_menu '10\n0\n'
contains "$CASE/out" 'does not exist; nothing to back up'
not_contains "$STATE/log" $'stop\t'
[ ! -d "$APP/backups" ] || fail 'backup directory created for absent volume'

fixture
run_menu '10\n0\n'
[ -f "$STATE/running" ] || fail 'backup did not restart previously running container'
archive=$(find "$APP/backups" -name ledger-six.tar.gz -type f)
[ -n "$archive" ] && is_private_file "$archive" || fail 'backup missing or not mode 600'
[ "$(tar xOzf "$archive" ./ledger.txt)" = 'original ledger' ] || fail 'backup did not contain data'
contains "$CASE/out" 'Verified backup:'

fixture
rm -f "$STATE/running"
run_menu '10\n0\n'
[ ! -f "$STATE/running" ] || fail 'backup started a previously stopped container'
not_contains "$STATE/log" $'stop\t'
not_contains "$STATE/log" $'start\t'

fixture
touch "$STATE/fail_archive"
run_menu '10\n0\n'
[ -f "$STATE/running" ] || fail 'failed backup did not restart running container'
[ -z "$(find "$APP/backups" -type f -name ledger-six.tar.gz)" ] || fail 'partial backup remained'
contains "$CASE/out" 'Archive creation failed'

fixture
touch "$STATE/other" "$STATE/other_running"
run_menu '10\n0\n'
contains "$CASE/out" 'Another running container'
not_contains "$STATE/log" $'stop\t'

fixture
touch "$STATE/slow_archive"
printf '10\n0\n' > "$CASE/input"
bash "$APP/manage.sh" < "$CASE/input" > "$CASE/out" 2>&1 &
pid=$!
for _ in 1 2 3 4 5 6 7 8 9 10 11 12 13 14 15 16 17 18 19 20; do
    grep -Fq $'tar\tczf' "$STATE/log" && break
    sleep 0.1
done
grep -Fq $'tar\tczf' "$STATE/log" || fail 'timed out waiting for backup'
kill -TERM "$pid"
wait "$pid" || :
[ -f "$STATE/running" ] || fail 'interrupted backup did not restart running container'
contains "$CASE/out" 'Backup interrupted'

fixture
printf 'invalid archive' > "$CASE/invalid.tar.gz"
run_menu "11\n$CASE/invalid.tar.gz\nRESTORE DATA\n0\n"
contains "$CASE/out" 'not a readable gzip tar archive'
[ -f "$STATE/running" ] && [ "$(cat "$STATE/data/ledger.txt")" = 'original ledger' ] || fail 'invalid archive touched data or stopped service'
not_contains "$STATE/log" $'stop\t'

fixture
mkdir "$CASE/restoredata"
printf 'restored ledger\n' > "$CASE/restoredata/ledger.txt"
tar czf "$CASE/trusted.tar.gz" -C "$CASE/restoredata" .
run_menu "11\n$CASE/trusted.tar.gz\nNO\n0\n"
[ -f "$STATE/running" ] && [ "$(cat "$STATE/data/ledger.txt")" = 'original ledger' ] || fail 'cancelled restore modified data'
not_contains "$STATE/log" $'stop\t'

fixture
mkdir "$CASE/restoredata"
printf 'restored ledger\n' > "$CASE/restoredata/ledger.txt"
tar czf "$CASE/trusted archive.tar.gz" -C "$CASE/restoredata" .
run_menu "11\n$CASE/trusted archive.tar.gz\nRESTORE DATA\n0\n"
[ -f "$STATE/running" ] || fail 'successful restore did not restart container'
[ "$(cat "$STATE/data/ledger.txt")" = 'restored ledger' ] || fail 'restore did not replace data'
pre=$(find "$APP/backups" -name ledger-six.tar.gz -type f)
[ "$(tar xOzf "$pre" ./ledger.txt)" = 'original ledger' ] || fail 'pre-restore backup missing original data'
contains "$CASE/out" 'Pre-restore backup:'
contains "$STATE/log" 'chown -R 10001:10001 /data'

fixture
rm -f "$STATE/volume" "$STATE/container" "$STATE/running"
mkdir "$CASE/restoredata"
printf 'new volume ledger\n' > "$CASE/restoredata/ledger.txt"
tar czf "$CASE/trusted.tar.gz" -C "$CASE/restoredata" .
run_menu "11\n$CASE/trusted.tar.gz\nRESTORE DATA\n0\n"
[ -f "$STATE/volume" ] && [ ! -f "$STATE/running" ] || fail 'missing volume restore state incorrect'
[ "$(cat "$STATE/data/ledger.txt")" = 'new volume ledger' ] || fail 'restore to new volume failed'
contains "$CASE/out" 'There is no existing volume to back up'

fixture
mkdir "$CASE/restoredata"
printf 'restored ledger\n' > "$CASE/restoredata/ledger.txt"
tar czf "$CASE/trusted.tar.gz" -C "$CASE/restoredata" .
touch "$STATE/fail_restore"
run_menu "11\n$CASE/trusted.tar.gz\nRESTORE DATA\n0\n"
[ ! -f "$STATE/running" ] || fail 'failed restore restarted service'
pre=$(find "$APP/backups" -name ledger-six.tar.gz -type f)
[ "$(tar xOzf "$pre" ./ledger.txt)" = 'original ledger' ] || fail 'failed restore did not retain pre-backup'
contains "$CASE/out" 'will NOT restart'
contains "$CASE/out" 'Pre-restore backup:'

fixture
mkdir "$CASE/restoredata"
printf 'restored ledger\n' > "$CASE/restoredata/ledger.txt"
tar czf "$CASE/trusted.tar.gz" -C "$CASE/restoredata" .
touch "$STATE/fail_archive"
run_menu "11\n$CASE/trusted.tar.gz\nRESTORE DATA\n0\n"
[ ! -f "$STATE/running" ] || fail 'pre-restore backup failure restarted service'
[ "$(cat "$STATE/data/ledger.txt")" = 'original ledger' ] || fail 'pre-restore backup failure modified data'
contains "$CASE/out" 'restore was not attempted'

fixture
printf 'secret\n' > "$APP/.env"
mkdir "$APP/backups"; printf 'keep\n' > "$APP/backups/old.tar.gz"
run_menu '13\nDELETE\n0\n'
[ -f "$STATE/volume" ] && [ -f "$STATE/container" ] || fail 'incorrect deletion phrase was accepted'
run_menu '12\n0\n'
[ -f "$STATE/volume" ] && [ ! -f "$STATE/container" ] || fail 'clean removal did not preserve volume'
run_menu '13\nDELETE DATA\n0\n'
[ ! -f "$STATE/volume" ] && [ ! -f "$STATE/image" ] || fail 'confirmed deletion failed'
[ -f "$APP/.env" ] && [ -f "$APP/backups/old.tar.gz" ] || fail 'deletion removed environment or backup'
not_contains "$STATE/log" --rmi
not_contains "$STATE/log" --volumes

fixture
touch "$STATE/other"
run_menu '13\nDELETE DATA\n0\n'
[ -f "$STATE/volume" ] && [ -f "$STATE/image" ] || fail 'shared resources were removed'
contains "$CASE/out" 'Other containers reference'

fixture
touch "$STATE/fail_volume_ls"
run_menu '10\n0\n'
contains "$CASE/out" 'refusing to assume the volume is absent'
not_contains "$STATE/log" $'stop\t'

# Real socket listeners exercise the bind checks; Docker remains mocked.
hold_port() {
    python3 - "$STATE/held-port" <<'PY' &
import socket, sys, time
s = socket.socket()
s.bind(('127.0.0.1', 0))
s.listen()
with open(sys.argv[1], 'w') as f:
    f.write(str(s.getsockname()[1]))
time.sleep(300)
PY
    PORT_HOLDERS="$PORT_HOLDERS $!"
    for _ in 1 2 3 4 5 6 7 8 9 10 11 12 13 14 15 16 17 18 19 20; do
        [ ! -s "$STATE/held-port" ] || break
        sleep 0.1
    done
    [ -s "$STATE/held-port" ] || fail 'socket holder failed to start'
    HELD=$(cat "$STATE/held-port")
}
free_port() {
    python3 -c 'import socket; s=socket.socket(); s.bind(("127.0.0.1",0)); print(s.getsockname()[1]); s.close()'
}

fixture
p=$(free_port)
printf 'APP_PORT=8080\nSECRET_KEY=secret-never-show\n' > "$APP/.env"
export APP_PORT=23456
run_menu "1\n2\n00$p\n0\n"
unset APP_PORT
contains "$APP/.env" "APP_PORT=$p"
contains "$APP/.env" 'SECRET_KEY=secret-never-show'
not_contains "$CASE/out" 'secret-never-show'
contains "$CASE/out" "Published URL: http://127.0.0.1:$p"
contains "$CASE/out" 'not a reservation'
is_private_file "$APP/.env" || fail 'manual deployment did not protect .env'
contains "$STATE/log" $'up\t-d\t--build\tledger'
[ ! -f "$STATE/shell_port_leaked" ] || fail 'external APP_PORT leaked into Compose'

fixture
hold_port
printf 'APP_PORT=%s\nSECRET_KEY=secret-never-show\n' "$HELD" > "$APP/.env"
cp "$APP/.env" "$CASE/before"
printf '8080\n' > "$STATE/port"
run_menu '1\n1\n0\n'
contains "$CASE/out" 'Cannot bind 127.0.0.1:'
not_contains "$STATE/log" $'up\t-d'
cmp -s "$APP/.env" "$CASE/before" || fail 'occupied configured port changed .env'
not_contains "$CASE/out" 'secret-never-show'
run_menu "9\n2\n$HELD\n0\n"
contains "$CASE/out" 'Cannot bind 127.0.0.1:'
cmp -s "$APP/.env" "$CASE/before" || fail 'occupied manual port changed .env'
not_contains "$STATE/log" $'stop\t'

fixture
hold_port
printf 'APP_PORT=%s\n' "$HELD" > "$APP/.env"
printf '%s\n' "$HELD" > "$STATE/port"
run_menu '1\n1\nNO\n0\n'
contains "$CASE/out" 'does not prove the local conflict belongs to that service'
not_contains "$STATE/log" $'up\t-d'
run_menu '1\n1\nREUSE\n0\n'
contains "$CASE/out" 'Reusing the reported service port'
contains "$STATE/log" $'up\t-d\t--build\tledger'
not_contains "$STATE/log" $'stop\t'
rm -f "$STATE/running"
: > "$STATE/log"
run_menu '1\n1\n0\n'
contains "$CASE/out" 'Cannot bind 127.0.0.1:'
not_contains "$STATE/log" $'up\t-d'

fixture
hold_port
printf 'APP_PORT=%s\nOTHER=preserved\n' "$HELD" > "$APP/.env"
run_menu '1\n3\n0\n'
auto=$(sed -n 's/^APP_PORT=//p' "$APP/.env")
case "$auto" in ''|*[!0-9]*) fail 'automatic selection did not write a numeric port' ;; esac
[ "$auto" != "$HELD" ] || fail 'automatic selection chose an occupied port'
contains "$CASE/out" "Automatically selected local port $auto"
contains "$CASE/out" "Published URL: http://127.0.0.1:$auto"
contains "$APP/.env" 'OTHER=preserved'
is_private_file "$APP/.env" || fail 'automatic selection did not protect .env'

fixture
p=$(free_port)
run_menu "9\n2\n$p\n0\n"
contains "$APP/.env" "APP_PORT=$p"
contains "$CASE/out" 'To apply a changed port'
not_contains "$STATE/log" $'up\t-d'
cp "$APP/.env" "$CASE/before"
run_menu '9\n2\nabc\n9\n2\n0\n9\n2\n65536\n0\n'
contains "$CASE/out" 'between 1 and 65535'
cmp -s "$APP/.env" "$CASE/before" || fail 'invalid manual input changed .env'

fixture
# The literal Compose interpolation expression is intentional.
# shellcheck disable=SC2016
printf 'APP_PORT=${OTHER_PORT}\nOTHER_PORT=12345\n' > "$APP/.env"
cp "$APP/.env" "$CASE/before"
run_menu '1\n1\n0\n'
contains "$CASE/out" 'Cannot keep an invalid or unresolved APP_PORT'
not_contains "$STATE/log" $'up\t-d'
cmp -s "$APP/.env" "$CASE/before" || fail 'unresolved port modified .env'

fixture
# Hide Python while retaining the commands required by the script and mock.
mkdir "$CASE/no-python"
for tool in bash dirname cat chmod awk mktemp mv mkdir date touch rm; do
    ln -s "$(command -v "$tool")" "$CASE/no-python/$tool"
done
saved_path=$PATH
export PATH="$CASE/bin:$CASE/no-python"
run_menu '1\n3\n1\n2\n23457\n1\n1\n0\n'
export PATH=$saved_path
contains "$CASE/out" 'Automatic selection requires a working Python 3 socket probe'
contains "$CASE/out" 'UNVERIFIED; Docker must check it at startup'
contains "$APP/.env" 'APP_PORT=23457'
[ "$(grep -Fc $'up\t-d\t--build\tledger' "$STATE/log")" = 2 ] || fail 'manual/default deployment should work without Python'

fixture
p=$(free_port)
touch "$STATE/fail_up"
run_menu "1\n2\n$p\n0\n"
contains "$CASE/out" 'Build or startup failed'
contains "$CASE/out" 'choose another port with option 9'
contains "$APP/.env" "APP_PORT=$p"
[ -f "$STATE/volume" ] && [ "$(cat "$STATE/data/ledger.txt")" = 'original ledger' ] || fail 'startup failure damaged data'
not_contains "$STATE/log" $'volume\trm'
not_contains "$STATE/log" $'stop\t'

fixture
p=$(free_port)
printf '127.0.0.1:54321\n' > "$STATE/reported_port"
run_menu "1\n2\n$p\n0\n"
contains "$CASE/out" 'Published URL: http://127.0.0.1:54321'
not_contains "$CASE/out" "Published URL: http://127.0.0.1:$p"
printf '0.0.0.0:54321\n' > "$STATE/reported_port"
run_menu '4\n0\n'
contains "$CASE/out" 'Could not confirm a single localhost published port'
not_contains "$CASE/out" 'Published URL:'

fixture
touch "$STATE/second"
run_menu '14\n0\n'
contains "$CASE/out" 'Logs can contain sensitive information'
contains "$CASE/out" 'no logs were deleted, uploaded, or shared'
contains "$CASE/out" 'logs must travel from that daemon to this machine'
not_contains "$CASE/out" 'sensitive-test-log'
logfile=$(find "$APP/backups/log-exports" -type f -name container-logs.txt)
[ -n "$logfile" ] && is_private_file "$logfile" || fail 'private log export missing or not mode 600'
[ "$(grep -c 'stdout for .*sensitive-test-log' "$logfile")" = 2 ] || fail 'not all container stdout was exported'
[ "$(grep -c 'stderr for ' "$logfile")" = 2 ] || fail 'not all container stderr was exported'
contains "$CASE/out" "Log export file: $logfile"
git -C "$SOURCE" check-ignore --no-index -q 'backups/log-exports/example/container-logs.txt' || fail 'log exports are not gitignored'
grep -qx 'backups/' "$SOURCE/.dockerignore" || fail 'log exports are not excluded from Docker build context'

fixture
touch "$STATE/fail_logs"
run_menu '14\n0\n'
contains "$CASE/out" 'export is incomplete'
logfile=$(find "$APP/backups/log-exports" -type f -name container-logs.txt)
[ -n "$logfile" ] && is_private_file "$logfile" || fail 'incomplete private logs were not retained'

fixture
rm -f "$STATE/container"
run_menu '14\n0\n'
contains "$CASE/out" 'No Compose project containers were found'
[ ! -d "$APP/backups/log-exports" ] || fail 'empty log export created a directory'

fixture
mkdir "$CASE/outside"
ln -s "$CASE/outside" "$APP/backups"
run_menu '14\n0\n'
contains "$CASE/out" 'Refusing to write logs through a symlinked'
[ -z "$(ls -A "$CASE/outside")" ] || fail 'logs escaped via symlink'

# Exercise real Git fast-forward behavior entirely in temporary repositories.
fixture
printf '.env\nbackups/\n' > "$APP/.gitignore"
git init -q --bare "$CASE/origin.git"
git -C "$APP" init -q
git -C "$APP" add .
git -C "$APP" -c user.name=Test -c user.email=test@example.invalid commit -qm initial
git -C "$APP" remote add origin "$CASE/origin.git"
git -C "$APP" push -qu origin HEAD
git clone -q "$CASE/origin.git" "$CASE/upstream"
printf '2.0.0\n' > "$CASE/upstream/VERSION"
git -C "$CASE/upstream" add VERSION
git -C "$CASE/upstream" -c user.name=Test -c user.email=test@example.invalid commit -qm update
git -C "$CASE/upstream" push -q
run_menu '2\n0\n'
contains "$CASE/out" "Version before pull: $SOURCE_VERSION"
contains "$CASE/out" 'Version after pull: 2.0.0'
contains "$CASE/out" 'Git update applied.'
contains "$STATE/log" $'up\t-d\t--build\tledger'
printf 'dirty\n' >> "$APP/VERSION"
: > "$STATE/log"
run_menu '2\n0\n'
contains "$CASE/out" 'Tracked changes exist'
not_contains "$STATE/log" $'up\t-d'

fixture
git -C "$APP" init -q
git -C "$APP" add .
git -C "$APP" -c user.name=Test -c user.email=test@example.invalid commit -qm initial
run_menu '2\n0\n'
contains "$CASE/out" 'no configured Git upstream'
not_contains "$STATE/log" $'up\t-d'

printf 'PASS: %s isolated cases, including real local Git and mocked Docker.\n' "$COUNT"
