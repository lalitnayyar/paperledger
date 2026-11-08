#!/usr/bin/env bash
# Interactive administration for the Ledger Six Docker Compose application.
# Bash 3.2+; Docker Engine and Docker Compose v2 are required for Docker actions.
set -u
set -o pipefail
set -f
umask 077

ROOT=$(CDPATH= cd -P "$(dirname "${BASH_SOURCE[0]}")" 2>/dev/null && pwd -P) || {
    printf '%s\n' 'Cannot determine the application directory.' >&2
    exit 1
}
cd "$ROOT" || exit 1
VOLUME=ledger-six-data
IMAGE=ledger-six:local
SERVICE=ledger
BACKUP_DIR="$ROOT/backups"
PARTIAL_ARCHIVE=
TEMP_ENV=
LAST_BACKUP=
PRE_RESTORE_BACKUP=
RESTART_IDS=
ALL_APP_IDS=
RUNNING_IDS=
BACKUP_RESTART_PENDING=0
RESTORE_IN_PROGRESS=0

say_error() { printf 'ERROR: %s\n' "$*" >&2; }
version() {
    if [ -r "$ROOT/VERSION" ]; then
        # VERSION is public application metadata, not environment configuration.
        cat "$ROOT/VERSION"
    else
        printf '%s' 'unknown (VERSION not readable)'
    fi
}
compose() {
    # Compose normally prioritizes shell variables over .env. APP_PORT changes in
    # the protected .env must be effective for commands run by this script.
    if [ -f "$ROOT/.env" ]; then
        ( unset APP_PORT; docker compose --env-file "$ROOT/.env" --project-directory "$ROOT" -f "$ROOT/compose.yaml" "$@" )
    else
        ( unset APP_PORT COMPOSE_ENV_FILES COMPOSE_DISABLE_ENV_FILE; docker compose --project-directory "$ROOT" -f "$ROOT/compose.yaml" "$@" )
    fi
}
docker_helper() {
    # Prevent Git Bash/MSYS from rewriting /data in container-only arguments.
    # Do not apply this to Compose: its project and file paths are host paths.
    MSYS_NO_PATHCONV=1 MSYS2_ARG_CONV_EXCL='*' docker run "$@"
}
check_docker() {
    if ! command -v docker >/dev/null 2>&1; then
        say_error 'Docker is not installed or not in PATH.'
        return 1
    fi
    if ! docker compose version >/dev/null 2>&1; then
        say_error 'Docker Compose v2 (docker compose) is required.'
        return 1
    fi
    if ! docker info >/dev/null 2>&1; then
        say_error 'Cannot access the Docker daemon. Start Docker and check your user permissions; this script does not assume root.'
        return 1
    fi
    if [ ! -f "$ROOT/compose.yaml" ]; then
        say_error 'compose.yaml is missing from the script directory.'
        return 1
    fi
    return 0
}
create_env_if_missing() {
    if [ -e "$ROOT/.env" ] || [ -L "$ROOT/.env" ]; then
        if [ ! -f "$ROOT/.env" ]; then
            say_error '.env exists but is not a regular file.'
            return 1
        fi
        return 0
    fi
    if [ ! -f "$ROOT/.env.example" ]; then
        say_error '.env.example is missing; cannot create .env.'
        return 1
    fi
    # noclobber prevents replacing a file that appeared after the existence check.
    if ! ( set -C; cat "$ROOT/.env.example" > "$ROOT/.env" ) 2>/dev/null; then
        say_error 'Could not create .env without overwriting an existing file.'
        return 1
    fi
    if ! chmod 600 "$ROOT/.env"; then
        say_error 'Created .env but could not protect it with mode 600; secure it before deploying.'
        return 1
    fi
    printf '%s\n' 'Created .env from .env.example with mode 600.'
    return 0
}

# Record only IDs in this Compose service, and separately those actually running.
# Full IDs avoid ambiguity between docker compose ps and docker ps abbreviations.
get_app_containers() {
    ALL_APP_IDS=
    RUNNING_IDS=
    local ids id info full running
    if ! ids=$(compose ps --all --quiet "$SERVICE"); then
        say_error 'Could not inspect the Compose service containers.'
        return 1
    fi
    for id in $ids; do
        if ! info=$(docker inspect --format '{{.Id}} {{.State.Running}}' "$id"); then
            say_error 'A service container could not be inspected; no data operation was attempted.'
            return 1
        fi
        full=${info%% *}
        running=${info#* }
        case "$full" in ''|*[!0-9a-f]*) say_error 'Unexpected Docker container ID.'; return 1 ;; esac
        case "$running" in true|false) ;; *) say_error 'Unexpected container state.'; return 1 ;; esac
        ALL_APP_IDS="$ALL_APP_IDS $full"
        if [ "$running" = true ]; then RUNNING_IDS="$RUNNING_IDS $full"; fi
    done
    return 0
}
# A failed inspect is not proof of absence: Docker may have disconnected. List
# names and distinguish an empty result from a failed daemon operation.
get_volume_state() {
    local names name
    VOLUME_EXISTS=0
    if ! names=$(docker volume ls --format '{{.Name}}'); then
        say_error 'Cannot list Docker volumes; refusing to assume the volume is absent.'
        return 1
    fi
    for name in $names; do
        if [ "$name" = "$VOLUME" ]; then VOLUME_EXISTS=1; break; fi
    done
    return 0
}
get_image_state() {
    local names name
    IMAGE_EXISTS=0
    if ! names=$(docker image ls --format '{{.Repository}}:{{.Tag}}'); then
        say_error 'Cannot list Docker images; refusing to assume the image is absent.'
        return 1
    fi
    for name in $names; do
        if [ "$name" = "$IMAGE" ]; then IMAGE_EXISTS=1; break; fi
    done
    return 0
}

# Refuse a consistent backup or restore while a different running container uses
# the globally named volume. Docker cannot detect non-container consumers.
check_no_other_running_volume_users() {
    local ids id
    if ! ids=$(docker ps --no-trunc --quiet --filter "volume=$VOLUME"); then
        say_error 'Cannot inspect running volume users.'
        return 1
    fi
    for id in $ids; do
        case " $ALL_APP_IDS " in
            *" $id "*) ;;
            *) say_error 'Another running container uses ledger-six-data. Stop it before backup or restore.'; return 1 ;;
        esac
    done
    return 0
}
check_volume_quiescent() {
    local ids
    if ! ids=$(docker ps --no-trunc --quiet --filter "volume=$VOLUME"); then
        say_error 'Cannot verify that the data volume is quiescent.'
        return 1
    fi
    if [ -n "$ids" ]; then
        say_error 'A running container still uses ledger-six-data; refusing the data operation.'
        return 1
    fi
    return 0
}
stop_initially_running() {
    local id
    RESTART_IDS=
    for id in $RUNNING_IDS; do
        # Record before stopping, so an interrupt or partial stop can be recovered.
        RESTART_IDS="$RESTART_IDS $id"
        if ! docker stop "$id" >/dev/null; then
            say_error "Could not stop service container $id."
            return 1
        fi
    done
    return 0
}
restart_initially_running() {
    local id running failed=0
    for id in $RESTART_IDS; do
        if ! running=$(docker inspect --format '{{.State.Running}}' "$id"); then
            say_error "Cannot inspect container $id; check and start it manually if needed."
            failed=1
        elif [ "$running" = true ]; then
            :
        elif [ "$running" = false ]; then
            if ! docker start "$id" >/dev/null; then
                say_error "Could not restart container $id; start it manually."
                failed=1
            fi
        else
            say_error "Unexpected state for container $id; check it manually."
            failed=1
        fi
    done
    return "$failed"
}
restore_failure_notice() {
    printf '%s\n' 'Restore did not complete. The script will NOT restart the application; inspect it before starting.' >&2
    if [ -n "$PRE_RESTORE_BACKUP" ]; then
        printf 'Pre-restore backup: %s\n' "$PRE_RESTORE_BACKUP" >&2
    elif [ -n "$LAST_BACKUP" ] && [ -f "$LAST_BACKUP" ]; then
        printf 'Pre-restore backup: %s\n' "$LAST_BACKUP" >&2
    else
        printf '%s\n' 'No completed pre-restore backup is available from this attempt; check existing backups. Data extraction was not started unless otherwise reported.' >&2
    fi
}
cleanup_on_exit() {
    local status=$?
    trap - EXIT INT TERM
    if [ -n "$PARTIAL_ARCHIVE" ]; then
        rm -f "$PARTIAL_ARCHIVE" || say_error 'Could not remove the incomplete archive.'
    fi
    if [ -n "$TEMP_ENV" ]; then
        rm -f "$TEMP_ENV" || say_error 'Could not remove the temporary .env file.'
    fi
    if [ "$BACKUP_RESTART_PENDING" -eq 1 ]; then
        printf '%s\n' 'Backup interrupted; attempting to restart containers that were running.' >&2
        if ! restart_initially_running; then status=1; fi
    fi
    if [ "$RESTORE_IN_PROGRESS" -eq 1 ]; then restore_failure_notice; fi
    exit "$status"
}
trap cleanup_on_exit EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

# All archive bytes are streamed over stdin/stdout. No host directory is mounted
# into the helper container, including for Docker Desktop and remote daemons.
archive_volume() {
    local dir archive stamp
    LAST_BACKUP=
    if ! mkdir -p "$BACKUP_DIR" || ! chmod 700 "$BACKUP_DIR"; then
        say_error 'Cannot create or protect the backup directory.'
        return 1
    fi
    stamp=$(date '+%Y%m%d-%H%M%S') || return 1
    if ! dir=$(mktemp -d "$BACKUP_DIR/ledger-six-$stamp-XXXXXX"); then
        say_error 'Cannot create a unique backup directory.'
        return 1
    fi
    archive="$dir/ledger-six.tar.gz"
    PARTIAL_ARCHIVE=$archive
    if ! : > "$archive" || ! chmod 600 "$archive"; then
        say_error 'Cannot create or protect the backup archive.'
        rm -f "$archive"
        PARTIAL_ARCHIVE=
        return 1
    fi
    printf '%s\n' 'Archiving the stopped data volume (Alpine may need to be pulled)...'
    if ! docker_helper --rm --network none --read-only \
        --mount "type=volume,source=$VOLUME,target=/data,readonly" \
        alpine:3.20 tar czf - -C /data . > "$archive"; then
        say_error 'Archive creation failed; discarding the incomplete archive.'
        rm -f "$archive"
        PARTIAL_ARCHIVE=
        return 1
    fi
    if ! docker_helper --rm -i --network none --read-only alpine:3.20 \
        tar tzf - < "$archive" >/dev/null; then
        say_error 'Archive verification failed; discarding the incomplete archive.'
        rm -f "$archive"
        PARTIAL_ARCHIVE=
        return 1
    fi
    LAST_BACKUP=$archive
    PARTIAL_ARCHIVE=
    if [ "$RESTORE_IN_PROGRESS" -eq 1 ]; then PRE_RESTORE_BACKUP=$archive; fi
    printf 'Verified backup: %s\n' "$archive"
    return 0
}
backup_notes() {
    printf '%s\n' \
        'Archives are sensitive: they contain private ledger data and may contain the generated encryption key.' \
        'Back up .env and any separately supplied APP_ENCRYPTION_KEY securely and separately; keep the original key for restores.' \
        'Protect archives and keys; do not upload them to public repositories.' \
        'Do not run simultaneous deployments or other volume writers during backup, restore, or deletion.'
}
do_backup() {
    check_docker || return 1
    get_volume_state || return 1
    if [ "$VOLUME_EXISTS" -ne 1 ]; then say_error 'The ledger-six-data volume does not exist; nothing to back up.'; return 1; fi
    get_app_containers || return 1
    check_no_other_running_volume_users || return 1
    backup_notes
    BACKUP_RESTART_PENDING=1
    RESTART_IDS=
    if ! stop_initially_running || ! check_volume_quiescent; then
        printf '%s\n' 'Backup aborted; attempting to restart previously running service containers.' >&2
        restart_initially_running || :
        BACKUP_RESTART_PENDING=0
        return 1
    fi
    local result=0
    if ! get_volume_state || [ "$VOLUME_EXISTS" -ne 1 ]; then
        say_error 'The data volume cannot be confirmed; backup was not attempted.'
        result=1
    else
        archive_volume || result=1
    fi
    if [ -n "$RESTART_IDS" ]; then printf '%s\n' 'Restarting the containers that were running before backup...'; fi
    restart_initially_running || result=1
    BACKUP_RESTART_PENDING=0
    return "$result"
}
do_restore() {
    check_docker || return 1
    printf '%s\n' 'Restore ONLY a trusted tar.gz archive: tar readability does not make an archive safe.' \
        'Restoring replaces ALL contents of the named ledger-six-data volume; .env is not restored.' \
        'The original separately stored encryption key and .env must be backed up/restored separately.'
    printf 'Archive path (absolute, or relative to this project; blank cancels): '
    local archive confirm initial_volume_state result=0
    IFS= read -r archive || return 1
    [ -n "$archive" ] || { printf '%s\n' 'Restore cancelled.'; return 0; }
    if [ ! -f "$archive" ] || [ ! -r "$archive" ]; then say_error 'Archive is not a readable regular file.'; return 1; fi
    printf '%s\n' 'Checking that the archive can be read (this does not establish trust)...'
    if ! docker_helper --rm -i --network none --read-only alpine:3.20 tar tzf - < "$archive" >/dev/null; then
        say_error 'The archive is not a readable gzip tar archive; no data was touched.'
        return 1
    fi
    get_app_containers || return 1
    check_no_other_running_volume_users || return 1
    get_volume_state || return 1
    initial_volume_state=$VOLUME_EXISTS
    if [ "$VOLUME_EXISTS" -eq 1 ]; then
        printf '%s\n' 'A new backup of the existing volume will be created before replacement.'
    else
        printf '%s\n' 'The named volume does not exist; it will be created. There is no existing volume to back up.'
    fi
    printf 'Type RESTORE DATA to replace the volume, or anything else to cancel: '
    IFS= read -r confirm || return 1
    if [ "$confirm" != 'RESTORE DATA' ]; then printf '%s\n' 'Restore cancelled; no data changed.'; return 0; fi
    PRE_RESTORE_BACKUP=
    LAST_BACKUP=
    RESTORE_IN_PROGRESS=1
    if ! stop_initially_running || ! check_volume_quiescent; then
        restore_failure_notice
        RESTORE_IN_PROGRESS=0
        return 1
    fi
    if ! get_volume_state; then
        restore_failure_notice
        RESTORE_IN_PROGRESS=0
        return 1
    fi
    if [ "$initial_volume_state" -eq 1 ] && [ "$VOLUME_EXISTS" -ne 1 ]; then
        say_error 'The existing data volume disappeared; no restore was attempted.'
        restore_failure_notice
        RESTORE_IN_PROGRESS=0
        return 1
    fi
    if [ "$VOLUME_EXISTS" -eq 1 ]; then
        if ! archive_volume; then
            say_error 'Pre-restore backup failed; restore was not attempted.'
            restore_failure_notice
            RESTORE_IN_PROGRESS=0
            return 1
        fi
        PRE_RESTORE_BACKUP=$LAST_BACKUP
    else
        if ! docker volume create "$VOLUME" >/dev/null; then
            say_error 'Could not create the named volume; restore was not attempted.'
            restore_failure_notice
            RESTORE_IN_PROGRESS=0
            return 1
        fi
    fi
    if ! check_volume_quiescent; then
        say_error 'A container began using the volume; no extraction was attempted.'
        restore_failure_notice
        RESTORE_IN_PROGRESS=0
        return 1
    fi
    printf '%s\n' 'Replacing data in the explicitly mounted volume; do not interrupt...'
    # Only /data is writable. Never interpolate host paths or the archive name
    # into this command. The archive must be trusted, not merely readable.
    if ! docker_helper --rm -i --network none --read-only \
        --mount "type=volume,source=$VOLUME,target=/data" alpine:3.20 \
        sh -c 'set -eu; cd /data; find . -mindepth 1 -maxdepth 1 -exec rm -rf -- {} +; left=$(find . -mindepth 1 -maxdepth 1 -print); [ -z "$left" ] || { echo "Could not clear volume" >&2; exit 1; }; tar xzf - -C /data; chown -R 10001:10001 /data' < "$archive"; then
        say_error 'Extraction or ownership repair failed; the volume may be incomplete.'
        restore_failure_notice
        RESTORE_IN_PROGRESS=0
        return 1
    fi
    RESTORE_IN_PROGRESS=0
    printf '%s\n' 'Restore and ownership repair completed.'
    if [ -n "$PRE_RESTORE_BACKUP" ]; then printf 'Pre-restore backup: %s\n' "$PRE_RESTORE_BACKUP"; fi
    if [ -n "$RESTART_IDS" ]; then printf '%s\n' 'Restarting only containers that were running before restore...'; fi
    restart_initially_running || result=1
    if [ "$result" -ne 0 ]; then say_error 'Data was restored, but the application did not fully restart. Check Docker and start it manually.'; fi
    return "$result"
}

do_deploy() {
    check_docker || return 1
    create_env_if_missing || return 1
    printf '%s\n' 'Building and starting Ledger Six; the existing data volume is preserved.'
    if ! compose up -d --build "$SERVICE"; then say_error 'Build or startup failed.'; return 1; fi
    compose ps --all "$SERVICE" || return 1
    printf '%s\n' 'Service is configured to bind to 127.0.0.1 only. Use Status to check its local port and health.'
}
do_update() {
    local before after head_before head_after
    if ! command -v git >/dev/null 2>&1; then say_error 'Git is required to update.'; return 1; fi
    # Require a real checkout rooted here; a ZIP inside an unrelated repository is not a checkout.
    if [ ! -e "$ROOT/.git" ] || ! git -C "$ROOT" rev-parse --is-inside-work-tree >/dev/null 2>&1; then
        say_error 'Update requires a Git checkout rooted here, not an extracted ZIP. Obtain a fresh ZIP or clone the repository.'
        return 1
    fi
    if ! git -C "$ROOT" symbolic-ref -q --short HEAD >/dev/null; then say_error 'A checked-out branch is required; detached HEAD cannot be updated.'; return 1; fi
    if ! git -C "$ROOT" diff --quiet --ignore-submodules=none || ! git -C "$ROOT" diff --cached --quiet --ignore-submodules=none; then
        say_error 'Tracked changes exist or could not be checked. Commit or stash tracked changes before updating.'
        return 1
    fi
    if ! git -C "$ROOT" rev-parse --abbrev-ref --symbolic-full-name '@{upstream}' >/dev/null 2>&1; then
        say_error 'The current branch has no configured Git upstream.'
        return 1
    fi
    check_docker || return 1
    before=$(version)
    head_before=$(git -C "$ROOT" rev-parse HEAD) || return 1
    printf 'Version before pull: %s\n' "$before"
    if ! git -C "$ROOT" pull --ff-only; then say_error 'Git pull failed; no rebuild was attempted.'; return 1; fi
    after=$(version)
    head_after=$(git -C "$ROOT" rev-parse HEAD) || return 1
    printf 'Version after pull: %s\n' "$after"
    if [ "$head_before" = "$head_after" ]; then
        printf '%s\n' 'No new Git commit was pulled.'
    else
        printf '%s\n' 'Git update applied.'
        if [ "$before" = "$after" ]; then printf '%s\n' 'VERSION did not change despite the new commit.'; fi
    fi
    create_env_if_missing || return 1
    if ! compose up -d --build "$SERVICE"; then say_error 'Code was pulled, but rebuild/start failed; data was preserved.'; return 1; fi
    printf '%s\n' 'Rebuild and startup completed.'
}
do_status() {
    check_docker || return 1
    compose ps --all "$SERVICE" || return 1
    local endpoint port
    if ! command -v curl >/dev/null 2>&1; then
        printf '%s\n' 'curl is not installed; local HTTP health check skipped.'
        return 0
    fi
    if ! endpoint=$(compose port "$SERVICE" 8000 2>/dev/null); then endpoint=; fi
    # Only probe loopback, never an arbitrary endpoint returned by Docker.
    endpoint=${endpoint%%$'\n'*}
    port=${endpoint##*:}
    case "$port" in ''|*[!0-9]*) printf '%s\n' 'No published local port found; local HTTP health check skipped.'; return 0 ;; esac
    if [ "${#port}" -gt 5 ] || [ "$port" -lt 1 ] || [ "$port" -gt 65535 ]; then
        printf '%s\n' 'Invalid published port; local HTTP health check skipped.'
        return 0
    fi
    if curl --noproxy '*' --fail --silent --show-error --max-time 5 -o /dev/null "http://127.0.0.1:$port/api/health"; then
        printf 'Local health OK: http://127.0.0.1:%s/api/health\n' "$port"
    else
        say_error "Local health check failed on 127.0.0.1:$port."
        return 1
    fi
}
do_port() {
    local port temp
    create_env_if_missing || return 1
    if [ -L "$ROOT/.env" ]; then say_error 'Refusing to replace a symlinked .env. Edit it securely by hand.'; return 1; fi
    printf 'New APP_PORT (1-65535, blank cancels): '
    IFS= read -r port || return 1
    [ -n "$port" ] || { printf '%s\n' 'Port change cancelled.'; return 0; }
    case "$port" in *[!0-9]*) say_error 'Port must contain decimal digits only.'; return 1 ;; esac
    # Avoid shell arithmetic octal interpretation and overly large numeric input.
    while [ "${#port}" -gt 1 ] && [ "${port#0}" != "$port" ]; do port=${port#0}; done
    if [ "${#port}" -gt 5 ] || [ "$port" -lt 1 ] || [ "$port" -gt 65535 ]; then say_error 'Port must be between 1 and 65535.'; return 1; fi
    if ! temp=$(mktemp "$ROOT/.env.manage-XXXXXX"); then say_error 'Could not create a protected temporary .env.'; return 1; fi
    TEMP_ENV=$temp
    if ! chmod 600 "$temp"; then rm -f "$temp"; TEMP_ENV=; say_error 'Could not protect temporary .env.'; return 1; fi
    # Preserve every other line, including lines within single-quoted multiline
    # Compose values. Refuse a multiline APP_PORT instead of corrupting it.
    if ! awk -v new="$port" '
        BEGIN { q=sprintf("%c",39); multi=0; found=0; bad=0 }
        function closes(s, start,    i,c,esc) {
            esc=0
            for (i=start; i<=length(s); i++) {
                c=substr(s,i,1)
                if (esc) { esc=0; continue }
                if (c=="\\") { esc=1; continue }
                if (c==q) return 1
            }
            return 0
        }
        {
            if (multi) { print; if (closes($0,1)) multi=0; next }
            if ($0 ~ /^[[:space:]]*(export[[:space:]]+)?APP_PORT[[:space:]]*(#.*)?$/) {
                cr=($0 ~ /\r$/ ? "\r" : "")
                print "APP_PORT=" new cr; found=1; next
            }
            if (match($0,/^[[:space:]]*(export[[:space:]]+)?[A-Za-z_][A-Za-z0-9_]*[[:space:]]*[:=][[:space:]]*/)) {
                prefix=substr($0,1,RLENGTH); value=substr($0,RLENGTH+1)
                quoted=(substr(value,1,1)==q)
                isport=(prefix ~ /^[[:space:]]*(export[[:space:]]+)?APP_PORT[[:space:]]*[:=]/)
                if (isport) {
                    if (quoted && !closes(value,2)) { bad=1; exit 2 }
                    cr=($0 ~ /\r$/ ? "\r" : "")
                    print "APP_PORT=" new cr; found=1; next
                }
                if (quoted && !closes(value,2)) multi=1
            }
            print
        }
        END { if (multi) exit 2; if (!bad && !found) print "APP_PORT=" new }
    ' "$ROOT/.env" > "$temp"; then
        rm -f "$temp"
        TEMP_ENV=
        say_error 'Could not safely update .env (a multiline APP_PORT is not supported); original file was preserved.'
        return 1
    fi
    if ! mv -f "$temp" "$ROOT/.env"; then rm -f "$temp"; TEMP_ENV=; say_error 'Could not replace .env; original file was preserved.'; return 1; fi
    TEMP_ENV=
    printf 'APP_PORT set to %s in protected .env. All other entries were retained.\n' "$port"
    printf '%s\n' 'To apply the port, choose install/build/start; existing data is preserved. The included Compose file binds 127.0.0.1 only.'
    printf '%s\n' 'This script ignores a shell-exported APP_PORT when running Compose. External Compose invocations may override .env.'
}
do_clean() {
    check_docker || return 1
    printf '%s\n' "Removing this Compose project's containers and network; the data volume, image, .env and backups are preserved."
    if ! compose down; then say_error 'Compose removal failed or was incomplete.'; return 1; fi
    printf '%s\n' 'Clean removal completed; data was preserved.'
}
do_delete() {
    check_docker || return 1
    printf '%s\n' \
        "DANGER: This removes this Compose project's containers, the ledger-six:local image tag, and the entire ledger-six-data volume." \
        'Ledger data and any generated in-volume encryption key will be permanently lost. .env and backups are preserved.' \
        'The image tag and named volume are global: make sure NO other project depends on them, including projects not currently running.'
    backup_notes
    printf 'Type DELETE DATA to confirm permanent deletion, or anything else to cancel: '
    local confirm ids failed=0
    IFS= read -r confirm || return 1
    if [ "$confirm" != 'DELETE DATA' ]; then printf '%s\n' 'Deletion cancelled; no changes made.'; return 0; fi
    # Never use down -v, --rmi, --remove-orphans, or global prune. Refuse to
    # remove globally named resources still referenced by any container.
    if ! compose down; then say_error 'Compose removal failed; volume and image were not touched.'; return 1; fi
    if ! get_volume_state; then
        say_error 'Volume was not touched.'; failed=1
    elif [ "$VOLUME_EXISTS" -eq 1 ]; then
        if ! ids=$(docker ps --all --no-trunc --quiet --filter "volume=$VOLUME"); then
            say_error 'Cannot check volume consumers; volume was preserved.'; failed=1
        elif [ -n "$ids" ]; then
            say_error 'Other containers reference ledger-six-data; volume was preserved.'; failed=1
        elif ! docker volume rm "$VOLUME"; then
            say_error 'Could not remove the named volume; it was preserved or removal was incomplete.'; failed=1
        fi
    else
        printf '%s\n' 'Named volume was already absent.'
    fi
    if ! get_image_state; then
        say_error 'Image was not touched.'; failed=1
    elif [ "$IMAGE_EXISTS" -eq 1 ]; then
        if ! ids=$(docker ps --all --no-trunc --quiet --filter "ancestor=$IMAGE"); then
            say_error 'Cannot check image consumers; image was preserved.'; failed=1
        elif [ -n "$ids" ]; then
            say_error 'Other containers reference ledger-six:local; image tag was preserved.'; failed=1
        elif ! docker image rm "$IMAGE"; then
            say_error 'Could not remove the application image tag.'; failed=1
        fi
    else
        printf '%s\n' 'Application image was already absent.'
    fi
    if [ "$failed" -eq 0 ]; then printf '%s\n' 'Deletion completed. .env and backups were preserved.'; else say_error 'Deletion was incomplete; see messages above. .env and backups were preserved.'; fi
    return "$failed"
}
show_menu() {
    printf '\n===== Ledger Six administration =====\nCurrent VERSION: %s\n' "$(version)"
    cat <<'MENU'
1) Install / build / start       2) Git pull --ff-only / rebuild
3) Status and local health       4) Start existing service
5) Stop service                  6) Restart existing service
7) View logs                     8) Follow logs (Ctrl-C to stop)
9) Change APP_PORT              10) Back up data volume
11) Restore trusted backup      12) Clean removal (preserve data)
13) Full deletion (DANGER)       0) Exit / back
MENU
}
usage() {
    cat <<'EOF'
Usage: ./manage.sh
Interactive Docker Compose administration; run from any directory.
Only trusted backup archives may be restored. Back up .env and any external
encryption key separately. Archives and logs can contain sensitive information.
The named data volume and image tag are global and must not be shared with other
projects. Do not run concurrent administration or volume writers. Backups
require the alpine:3.20 image, pulled by Docker if absent.
EOF
}
case "${1-}" in
    -h|--help) usage; exit 0 ;;
    '') ;;
    *) usage >&2; exit 2 ;;
esac
if [ "$#" -gt 0 ]; then say_error 'Unexpected arguments.'; exit 2; fi
printf '%s\n' 'Local-only deployment: the included Compose file binds 127.0.0.1.' \
    'Do not share the fixed image tag or named data volume with other projects.'
backup_notes
while :; do
    show_menu
    printf 'Choose an option: '
    if ! IFS= read -r choice; then printf '\n'; break; fi
    case "$choice" in
        1) do_deploy || : ;;
        2) do_update || : ;;
        3) do_status || : ;;
        4) if check_docker; then compose start "$SERVICE" || say_error 'Start failed; deploy first if no container exists.'; fi ;;
        5) if check_docker; then compose stop "$SERVICE" || say_error 'Stop failed.'; fi ;;
        6) if check_docker; then compose restart "$SERVICE" || say_error 'Restart failed; deploy first if no container exists.'; fi ;;
        7) if check_docker; then printf '%s\n' 'Logs may contain sensitive information.'; compose logs --tail 200 "$SERVICE" || say_error 'Could not read logs.'; fi ;;
        8) if check_docker; then printf '%s\n' 'Logs may contain sensitive information. Press Ctrl-C to return or exit.'; compose logs --follow --tail 200 "$SERVICE" || :; fi ;;
        9) do_port || : ;;
        10) do_backup || : ;;
        11) do_restore || : ;;
        12) do_clean || : ;;
        13) do_delete || : ;;
        0|q|Q) printf '%s\n' 'Exiting; no additional changes made.'; break ;;
        *) printf '%s\n' 'Invalid choice.' ;;
    esac
done
