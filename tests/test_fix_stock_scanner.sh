#!/usr/bin/env bash
# Isolated launcher tests: deploy, Docker, Git, and network clients are stubs.
set -euo pipefail

fail() {
  printf 'FAIL: %s\n' "$1" >&2
  exit 1
}
assert_contains() {
  grep -F -q -- "$2" "$1" || fail "Expected output was missing: $2"
}
assert_absent() {
  if grep -F -q -- "$2" "$1"; then
    fail 'Unexpected or sensitive text appeared in output.'
  fi
}
mode() {
  # Read only the permission prefix; the filename is never parsed.
  # shellcheck disable=SC2012
  LC_ALL=C ls -ld "$1" | cut -c1-10
}

case ${BASH_SOURCE[0]} in
  */*) test_dir=${BASH_SOURCE[0]%/*} ;;
  *) test_dir=. ;;
esac
repo_dir=$(CDPATH='' cd -- "$test_dir/.." && pwd -P)
launcher="$repo_dir/fix-stock-scanner.sh"
bash -n "$launcher"
bash -n "${BASH_SOURCE[0]}"
[ -x "$launcher" ] || fail 'Launcher is not executable.'

raw_tmp=$(mktemp -d "${TMPDIR:-/tmp}/fix-stock-scanner-test.XXXXXX")
tmp_dir=$(CDPATH='' cd -- "$raw_tmp" && pwd -P)
trap 'rm -rf "$tmp_dir"' EXIT
project="$tmp_dir/Project - with spaces 'quoted'"
outside="$tmp_dir/outside"
mock_bin="$tmp_dir/mock bin"
record="$tmp_dir/deploy-record"
mock_log="$tmp_dir/mock-log"
stdout="$tmp_dir/stdout"
stderr="$tmp_dir/stderr"
expected="$tmp_dir/expected"
mkdir -p "$project" "$outside" "$mock_bin"
cp "$launcher" "$project/fix-stock-scanner.sh"
chmod +x "$project/fix-stock-scanner.sh"

cat > "$project/deploy.sh" <<'EOF'
#!/usr/bin/env bash
{
  printf 'argc=%s\n' "$#"
  for arg in "$@"; do printf 'arg=%s\n' "$arg"; done
  printf 'cwd=%s\n' "$PWD"
  printf 'git_terminal_prompt=%s\n' "${GIT_TERMINAL_PROMPT-unset}"
  printf 'port=%s\n' "${APP_PORT-unset}"
  printf 'bind=%s\n' "${APP_BIND_ADDR-unset}"
  printf 'auth=%s\n' "${APP_AUTH_ENABLED-unset}"
  if [ "${APP_ENCRYPTION_KEY-unset}" = "$TEST_EXPECT_KEY" ]; then
    printf '%s\n' 'key=preserved'
  else
    printf '%s\n' 'key=changed'
  fi
  if IFS= read -r input; then
    printf '%s\n' 'stdin=not-eof'
  else
    printf '%s\n' 'stdin=eof'
  fi
} > "$TEST_RECORD"
printf '%s\n' 'Stub deployment ran.'
exit "${TEST_EXIT:-0}"
EOF

# Any direct Docker, Git, or external network command is a test failure.
for command_name in docker git curl wget ssh nc; do
  cat > "$mock_bin/$command_name" <<'EOF'
#!/usr/bin/env bash
printf '%s\n' "${0##*/}" >> "$TEST_MOCK_LOG"
exit 96
EOF
  chmod +x "$mock_bin/$command_name"
done

# This is intentionally not safe to source. The launcher must not read it,
# execute it, expose its contents, change its contents, or change its mode.
cat > "$project/.env" <<'EOF'
APP_PORT=27003
APP_BIND_ADDR=127.0.0.1
APP_ENCRYPTION_KEY=DO_NOT_EXPOSE_FILE_SECRET_48326
touch "$TEST_ENV_MARKER"
EOF
chmod 640 "$project/.env"
cp -p "$project/.env" "$tmp_dir/expected-env"
env_mode=$(mode "$project/.env")

run_launcher() {
  rm -f "$record" "$stdout" "$stderr"
  (
    cd -- "$outside"
    TEST_RECORD="$record" TEST_MOCK_LOG="$mock_log" \
    TEST_ENV_MARKER="$tmp_dir/env-was-sourced" TEST_EXIT="${TEST_EXIT:-0}" \
    TEST_EXPECT_KEY='DO_NOT_EXPOSE_SHELL_SECRET_48326' \
    APP_PORT=18476 APP_BIND_ADDR=127.0.0.77 APP_AUTH_ENABLED=1 \
    APP_ENCRYPTION_KEY='DO_NOT_EXPOSE_SHELL_SECRET_48326' PATH="$mock_bin:$PATH" \
      bash "$project/fix-stock-scanner.sh" "$@" <<'INPUT'
input-that-must-not-reach-deploy
INPUT
  ) > "$stdout" 2> "$stderr"
}

assert_record() {
  {
    printf 'argc=%s\n' "$#"
    for arg in "$@"; do printf 'arg=%s\n' "$arg"; done
    printf 'cwd=%s\n' "$project"
    printf '%s\n' 'git_terminal_prompt=0' 'port=18476' 'bind=127.0.0.77' \
      'auth=1' 'key=preserved' 'stdin=eof'
  } > "$expected"
  cmp -s "$expected" "$record" || fail 'Incorrect deployment arguments, directory, input, or inherited settings.'
}
assert_private() {
  for output in "$stdout" "$stderr"; do
    assert_absent "$output" 'DO_NOT_EXPOSE_FILE_SECRET_48326'
    assert_absent "$output" 'DO_NOT_EXPOSE_SHELL_SECRET_48326'
    assert_absent "$output" 'sensitive-value-48326'
  done
}
assert_env_unchanged() {
  cmp -s "$project/.env" "$tmp_dir/expected-env" || fail '.env contents changed.'
  [ "$(mode "$project/.env")" = "$env_mode" ] || fail '.env permissions changed.'
  [ ! -e "$tmp_dir/env-was-sourced" ] || fail '.env was sourced.'
}

# The no-argument form delegates exactly once, without update or pull.
run_launcher || fail 'Default invocation failed.'
assert_record
assert_contains "$stdout" 'Rebuilding and restarting via deploy.sh'
assert_contains "$stdout" 'No database volume will be deleted.'
assert_contains "$stdout" 'Stub deployment ran.'
assert_private
assert_env_unchanged

# Updating is delegated with exactly one --update argument.
run_launcher --update || fail 'Update invocation failed.'
assert_record --update
assert_private
assert_env_unchanged

run_launcher --help || fail 'Help invocation failed.'
[ ! -e "$record" ] || fail 'Help invoked deploy.sh.'
assert_contains "$stdout" 'Usage: bash fix-stock-scanner.sh'
assert_contains "$stdout" 'without pulling'
assert_contains "$stdout" 'fast-forward only'
assert_contains "$stdout" 'existing regular .env'
assert_contains "$stdout" 'APP_BIND_ADDR'
assert_contains "$stdout" 'database volume'
assert_private
assert_env_unchanged

# Do not print invalid arguments, which may contain credentials.
for invalid_case in unknown extra duplicate help_extra empty; do
  case "$invalid_case" in
    unknown) set -- '--unknown-sensitive-value-48326' ;;
    extra) set -- --update 'extra-sensitive-value-48326' ;;
    duplicate) set -- --update --update ;;
    help_extra) set -- --help 'extra-sensitive-value-48326' ;;
    empty) set -- '' ;;
  esac
  status=0
  run_launcher "$@" || status=$?
  [ "$status" -eq 2 ] || fail "Invalid arguments returned $status, not 2: $invalid_case"
  [ ! -e "$record" ] || fail 'Invalid arguments invoked deploy.sh.'
  [ ! -s "$stdout" ] || fail 'Invalid arguments produced stdout.'
  assert_contains "$stderr" 'Invalid arguments.'
  assert_private
  assert_env_unchanged
done

TEST_EXIT=37
status=0
run_launcher --update || status=$?
[ "$status" -eq 37 ] || fail "Update deployment status was $status, not 37."
assert_record --update
assert_private
assert_env_unchanged
TEST_EXIT=125
status=0
run_launcher || status=$?
[ "$status" -eq 125 ] || fail "Default deployment status was $status, not 125."
assert_record
assert_private
assert_env_unchanged
unset TEST_EXIT

# Refuse to run deploy.sh when it could create .env or when .env is unsafe.
mv "$project/.env" "$tmp_dir/saved-env"
for unsafe in missing symlink dangling_symlink directory fifo; do
  case "$unsafe" in
    missing) ;;
    symlink) ln -s "$tmp_dir/saved-env" "$project/.env" ;;
    dangling_symlink) ln -s "$tmp_dir/nonexistent-target" "$project/.env" ;;
    directory) mkdir "$project/.env" ;;
    fifo) mkfifo "$project/.env" ;;
  esac
  for option in default update; do
    status=0
    if [ "$option" = update ]; then
      run_launcher --update || status=$?
    else
      run_launcher || status=$?
    fi
    [ "$status" -eq 1 ] || fail "Unsafe .env returned $status for $unsafe/$option."
    [ ! -e "$record" ] || fail 'Unsafe .env invoked deploy.sh.'
    assert_contains "$stderr" 'existing regular .env file is required'
    assert_private
  done
  case "$unsafe" in
    missing) [ ! -e "$project/.env" ] || fail 'Missing .env was created.' ;;
    symlink) [ -L "$project/.env" ] || fail '.env symlink was changed.'; rm "$project/.env" ;;
    dangling_symlink)
      [ -L "$project/.env" ] || fail 'Dangling .env symlink was changed.'
      [ ! -e "$tmp_dir/nonexistent-target" ] || fail 'Dangling symlink target was created.'
      rm "$project/.env" ;;
    directory) [ -d "$project/.env" ] || fail '.env directory was changed.'; rmdir "$project/.env" ;;
    fifo) [ -p "$project/.env" ] || fail '.env FIFO was changed.'; rm "$project/.env" ;;
  esac
done
run_launcher --help || fail 'Help should work without .env.'
[ ! -e "$record" ] || fail 'Help without .env invoked deploy.sh.'
mv "$tmp_dir/saved-env" "$project/.env"
assert_env_unchanged

# Relative paths, executable invocation, and CDPATH must find the right checkout.
rm -f "$record"
(
  cd -- "$outside"
  TEST_RECORD="$record" TEST_MOCK_LOG="$mock_log" \
  TEST_ENV_MARKER="$tmp_dir/env-was-sourced" TEST_EXPECT_KEY='DO_NOT_EXPOSE_SHELL_SECRET_48326' \
  APP_PORT=18476 APP_BIND_ADDR=127.0.0.77 APP_AUTH_ENABLED=1 \
  APP_ENCRYPTION_KEY='DO_NOT_EXPOSE_SHELL_SECRET_48326' PATH="$mock_bin:$PATH" \
    "../Project - with spaces 'quoted'/fix-stock-scanner.sh" >/dev/null
) </dev/null
assert_record

decoy_root="$tmp_dir/cdpath trap"
mkdir -p "$decoy_root/Project - with spaces 'quoted'"
cp "$project/deploy.sh" "$decoy_root/Project - with spaces 'quoted'/deploy.sh"
rm -f "$record"
(
  cd -- "$tmp_dir"
  TEST_RECORD="$record" TEST_MOCK_LOG="$mock_log" \
  TEST_ENV_MARKER="$tmp_dir/env-was-sourced" TEST_EXPECT_KEY='DO_NOT_EXPOSE_SHELL_SECRET_48326' \
  APP_PORT=18476 APP_BIND_ADDR=127.0.0.77 APP_AUTH_ENABLED=1 \
  APP_ENCRYPTION_KEY='DO_NOT_EXPOSE_SHELL_SECRET_48326' CDPATH="$decoy_root" PATH="$mock_bin:$PATH" \
    bash "Project - with spaces 'quoted'/fix-stock-scanner.sh" >/dev/null
) </dev/null
assert_record

# A literal dash directory must not be interpreted as cd's OLDPWD shortcut.
dash_project="$tmp_dir/-"
mkdir -p "$dash_project"
cp "$launcher" "$dash_project/fix-stock-scanner.sh"
cp "$project/deploy.sh" "$dash_project/deploy.sh"
cp "$project/.env" "$dash_project/.env"
rm -f "$record"
(
  cd -- "$tmp_dir"
  TEST_RECORD="$record" TEST_MOCK_LOG="$mock_log" \
  TEST_ENV_MARKER="$tmp_dir/env-was-sourced" TEST_EXPECT_KEY='DO_NOT_EXPOSE_SHELL_SECRET_48326' \
  APP_PORT=18476 APP_BIND_ADDR=127.0.0.77 APP_AUTH_ENABLED=1 \
  APP_ENCRYPTION_KEY='DO_NOT_EXPOSE_SHELL_SECRET_48326' OLDPWD="$outside" PATH="$mock_bin:$PATH" \
    bash -- '-/fix-stock-scanner.sh' >/dev/null
) </dev/null
original_project=$project
project=$dash_project
assert_record
project=$original_project
assert_env_unchanged

[ ! -e "$mock_log" ] || fail 'Launcher invoked Docker, Git, or a network client directly.'
[ ! -e "$tmp_dir/env-was-sourced" ] || fail '.env was sourced.'
printf '%s\n' 'All fix-stock-scanner tests passed (stub deploy; no Docker or network).'
