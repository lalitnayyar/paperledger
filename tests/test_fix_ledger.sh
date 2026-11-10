#!/usr/bin/env bash
set -euo pipefail

fail() {
  printf 'FAIL: %s\n' "$1" >&2
  exit 1
}

assert_contains() {
  if ! grep -F -q -- "$2" "$1"; then
    fail "Expected output was missing: $2"
  fi
}

assert_absent() {
  if grep -F -q -- "$2" "$1"; then
    fail 'Unexpected or sensitive text appeared in output.'
  fi
}

case ${BASH_SOURCE[0]} in
  */*) test_dir=${BASH_SOURCE[0]%/*} ;;
  *) test_dir=. ;;
esac
repo_dir=$(cd -- "$test_dir/.." && pwd -P)
launcher="$repo_dir/fix-ledger.sh"
bash -n "$launcher"
bash -n "${BASH_SOURCE[0]}"

tmp_dir=$(mktemp -d "${TMPDIR:-/tmp}/fix-ledger-test.XXXXXX")
trap 'rm -rf -- "$tmp_dir"' EXIT
project="$tmp_dir/Project - with spaces 'quoted'"
outside="$tmp_dir/outside"
mock_bin="$tmp_dir/mock bin"
record="$tmp_dir/deploy-record"
mock_log="$tmp_dir/mock-log"
stdout="$tmp_dir/stdout"
stderr="$tmp_dir/stderr"
expected="$tmp_dir/expected"
mkdir -p "$project" "$outside" "$mock_bin"
cp -- "$launcher" "$project/fix-ledger.sh"

cat > "$project/deploy.sh" <<'EOF'
#!/usr/bin/env bash
{
  printf 'argc=%s\n' "$#"
  for arg in "$@"; do printf 'arg=%s\n' "$arg"; done
  printf 'cwd=%s\n' "$PWD"
  printf 'git_terminal_prompt=%s\n' "${GIT_TERMINAL_PROMPT-unset}"
  printf 'bind=%s\n' "${APP_BIND_ADDR-unset}"
  if IFS= read -r input; then
    printf '%s\n' 'stdin=not-eof'
  else
    printf '%s\n' 'stdin=eof'
  fi
} > "$TEST_RECORD"
printf '%s\n' 'Current application version: stub-before-version'
if [ "$#" -eq 1 ] && [ "$1" = '--update' ]; then
  printf '%s\n' 'Application version after pull: stub-after-version'
fi
exit "${TEST_EXIT:-0}"
EOF

# If the launcher tries to invoke either tool itself, record and fail the test.
cat > "$mock_bin/docker" <<'EOF'
#!/usr/bin/env bash
printf '%s\n' docker >> "$TEST_MOCK_LOG"
exit 96
EOF
cat > "$mock_bin/git" <<'EOF'
#!/usr/bin/env bash
printf '%s\n' git >> "$TEST_MOCK_LOG"
exit 96
EOF
chmod +x "$mock_bin/docker" "$mock_bin/git"

# Sourcing this file would expose the marker and execute a command. The launcher
# must not read it or print its contents.
cat > "$project/.env" <<'EOF'
LEDGER_SECRET=DO_NOT_EXPOSE_SECRET_48326
touch "$TEST_ENV_MARKER"
EOF

run_launcher() {
  rm -f -- "$record" "$stdout" "$stderr"
  (
    cd -- "$outside"
    TEST_RECORD="$record" TEST_MOCK_LOG="$mock_log" \
    TEST_ENV_MARKER="$tmp_dir/env-was-sourced" TEST_EXIT="${TEST_EXIT:-0}" \
    APP_BIND_ADDR=127.0.0.77 PATH="$mock_bin:$PATH" \
      bash "$project/fix-ledger.sh" "$@" <<'EOF'
input-that-must-not-reach-deploy
EOF
  ) > "$stdout" 2> "$stderr"
}

assert_record() {
  {
    printf 'argc=%s\n' "$#"
    for arg in "$@"; do printf 'arg=%s\n' "$arg"; done
    printf 'cwd=%s\n' "$project"
    printf '%s\n' 'git_terminal_prompt=0' 'bind=127.0.0.77' 'stdin=eof'
  } > "$expected"
  if ! cmp -s "$expected" "$record"; then
    fail 'The deployment arguments, working directory, input, or environment were incorrect.'
  fi
}

run_launcher || fail 'Default invocation failed.'
assert_record
assert_contains "$stdout" 'The script will build/restart the service via deploy.sh; no database volume deletion'
assert_contains "$stdout" 'Existing settings and accounts remain in place'
assert_contains "$stdout" 'configured APP_BIND_ADDR network binding is retained'
assert_contains "$stdout" 'Current application version: stub-before-version'
assert_absent "$stdout" 'stub-after-version'
assert_absent "$stdout" 'DO_NOT_EXPOSE_SECRET_48326'
assert_absent "$stderr" 'DO_NOT_EXPOSE_SECRET_48326'
[ ! -e "$tmp_dir/env-was-sourced" ] || fail '.env was sourced.'

run_launcher --update || fail 'Update invocation failed.'
assert_record --update
assert_contains "$stdout" 'Current application version: stub-before-version'
assert_contains "$stdout" 'Application version after pull: stub-after-version'

run_launcher --help || fail 'Help invocation failed.'
[ ! -e "$record" ] || fail 'Help invoked the deployment helper.'
assert_contains "$stdout" 'Usage: bash fix-ledger.sh'
assert_contains "$stdout" 'APP_BIND_ADDR'
assert_absent "$stdout" 'Current application version:'

# Invalid input is rejected without including any argument in the output.
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
  [ ! -e "$record" ] || fail 'Invalid arguments invoked the deployment helper.'
  [ ! -s "$stdout" ] || fail 'Invalid arguments produced stdout.'
  assert_contains "$stderr" 'Invalid arguments.'
  assert_absent "$stderr" 'sensitive-value-48326'
done

TEST_EXIT=37
status=0
run_launcher --update || status=$?
[ "$status" -eq 37 ] || fail "Deployment exit status was $status, not 37."
assert_record --update
unset TEST_EXIT

# Relative paths from a different working directory must select the same project.
rm -f -- "$record"
(
  cd -- "$outside"
  TEST_RECORD="$record" TEST_MOCK_LOG="$mock_log" \
  TEST_ENV_MARKER="$tmp_dir/env-was-sourced" APP_BIND_ADDR=127.0.0.77 \
  PATH="$mock_bin:$PATH" \
    bash "../Project - with spaces 'quoted'/fix-ledger.sh" >/dev/null
)
assert_record

# CDPATH must not redirect a relative project path to a different checkout.
decoy_root="$tmp_dir/cdpath trap"
mkdir -p "$decoy_root/Project - with spaces 'quoted'"
cp -- "$project/deploy.sh" "$decoy_root/Project - with spaces 'quoted'/deploy.sh"
rm -f -- "$record"
(
  cd -- "$tmp_dir"
  TEST_RECORD="$record" TEST_MOCK_LOG="$mock_log" \
  TEST_ENV_MARKER="$tmp_dir/env-was-sourced" APP_BIND_ADDR=127.0.0.77 \
  CDPATH="$decoy_root" PATH="$mock_bin:$PATH" \
    bash "Project - with spaces 'quoted'/fix-ledger.sh" >/dev/null
)
assert_record

# A literal dash is a directory name here, not the cd builtin's OLDPWD shortcut.
dash_project="$tmp_dir/-"
mkdir -p "$dash_project"
cp -- "$launcher" "$dash_project/fix-ledger.sh"
cp -- "$project/deploy.sh" "$dash_project/deploy.sh"
rm -f -- "$record"
(
  cd -- "$tmp_dir"
  TEST_RECORD="$record" TEST_MOCK_LOG="$mock_log" \
  TEST_ENV_MARKER="$tmp_dir/env-was-sourced" APP_BIND_ADDR=127.0.0.77 \
  OLDPWD="$outside" PATH="$mock_bin:$PATH" \
    bash -- '-/fix-ledger.sh' >/dev/null
)
original_project=$project
project=$dash_project
assert_record
project=$original_project

[ ! -e "$mock_log" ] || fail 'The launcher invoked Docker or Git directly.'
[ ! -e "$tmp_dir/env-was-sourced" ] || fail '.env was sourced.'

printf '%s\n' 'All fix-ledger tests passed.'
