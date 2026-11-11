#!/usr/bin/env bash
# Delegate the stock scanner patch deployment to the maintained helper.

usage() {
  cat <<'EOF'
Usage: bash fix-stock-scanner.sh [--update | --help]

With no arguments, rebuild and restart from the current checkout without pulling.
--update  Pull the current branch with fast-forward only, then rebuild; deploy.sh
          requires a clean tracked working tree and a configured upstream.
--help    Show this help without running deployment.

An existing regular .env file is required and is not changed. Existing port,
APP_BIND_ADDR network binding, settings, accounts, and encryption keys are
retained. No database volume is deleted. Deployment is handled by deploy.sh.
EOF
}

invalid_arguments() {
  printf '%s\n' 'Invalid arguments. Use --help for usage.' >&2
  exit 2
}

if [ "$#" -eq 1 ]; then
  case "$1" in
    --update) ;;
    --help) usage; exit 0 ;;
    *) invalid_arguments ;;
  esac
elif [ "$#" -ne 0 ]; then
  invalid_arguments
fi

# Select the checkout containing this launcher, not the caller's directory.
# Prefix relative paths so a literal '-' cannot be interpreted as OLDPWD.
case ${BASH_SOURCE[0]} in
  */*)
    project_dir=${BASH_SOURCE[0]%/*}
    [ -n "$project_dir" ] || project_dir=/
    ;;
  *) project_dir=. ;;
esac
case $project_dir in
  /*) ;;
  *) project_dir=./$project_dir ;;
esac
if ! CDPATH='' cd -- "$project_dir" 2>/dev/null; then
  printf '%s\n' 'Could not access the project directory.' >&2
  exit 1
fi

# deploy.sh creates .env from the example when it is absent. Refuse that path
# so this patch launcher cannot create or modify configuration. deploy.sh also
# rejects symlinks and non-regular files; enforce that before delegating.
if [ -L .env ] || [ ! -f .env ]; then
  printf '%s\n' 'An existing regular .env file is required; no changes were made.' >&2
  exit 1
fi

printf '%s\n' \
  'Rebuilding and restarting via deploy.sh; existing settings, keys, and data are retained.' \
  'No database volume will be deleted.'

# Do not allow Git to prompt for credentials or forward terminal input.
# exec preserves the helper's exit status and passes its arguments unchanged.
GIT_TERMINAL_PROMPT=0 exec bash ./deploy.sh "$@" </dev/null
