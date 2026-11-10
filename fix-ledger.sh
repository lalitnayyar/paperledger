#!/usr/bin/env bash
# Delegate deployment to the project's maintained helper.

usage() {
  cat <<'EOF'
Usage: bash fix-ledger.sh [--update | --help]

Rebuild and start the existing service using deploy.sh without pulling changes.
--update  Pull the current branch with fast-forward only before rebuilding;
          deploy.sh requires tracked changes to be clean and an upstream set.
--help    Show this help without running deployment.

Existing settings and accounts remain in place; the configured APP_BIND_ADDR
network binding is retained. No database volume is deleted.
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

# Change directory in this process only. Parameter expansion avoids interpreting
# spaces or leading dashes in the path and also handles a script in the root.
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

printf '%s\n' \
  'The script will build/restart the service via deploy.sh; no database volume deletion' \
  'Existing settings and accounts remain in place; the configured APP_BIND_ADDR network binding is retained.' \
  'deploy.sh reports the current application version before rebuilding, after its prerequisite checks.'

# Prevent terminal input and Git's own terminal prompts without changing the
# caller's environment. The helper remains responsible for the actual deploy.
GIT_TERMINAL_PROMPT=0 exec bash ./deploy.sh "$@" </dev/null
