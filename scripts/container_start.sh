#!/bin/sh
# Container start: make the data directories (the Railway volume) writable by the restricted `app` user, then run
# the service as that user. Railway mounts volumes owned by root, and `app` cannot chown them itself, so the
# container starts as root, and root is used ONLY for the steps below. The service process itself never runs as root.
set -eu

APP=app
if [ "$(id -u)" != "0" ]; then
    exec "$@"                       # already unprivileged (e.g. a local `docker run --user`): nothing to prepare
fi

DIRS="${SG_DATA_DIR:-}"
if [ -n "${SG_LEAGUE_DB:-}" ]; then
    DIRS="$DIRS $(dirname "$SG_LEAGUE_DB")"
fi

drop() {
    # Same flags for the checks and the service: uid/gid app, its groups only, no inheritable capabilities.
    HOME=/home/$APP USER=$APP LOGNAME=$APP setpriv --reuid="$APP" --regid="$APP" --init-groups --inh-caps=-all -- "$@"
}

for dir in $DIRS; do
    mkdir -p "$dir"
    # Only touch what isn't already app's (idempotent; fast on restarts). -xdev: never cross into other mounts.
    changed=$(find "$dir" -xdev \( ! -user "$APP" -o ! -group "$APP" \) -print -exec chown -h "$APP:$APP" {} + | wc -l)
    if ! drop test -w "$dir"; then
        echo "container_start: $dir is not writable by $APP after chown; refusing to start" >&2
        exit 1
    fi
    echo "container_start: $dir ready for $APP ($changed entries re-owned)"
done

echo "container_start: dropping to $APP: $(drop sh -c 'grep -E "^(Uid|Gid|CapEff)" /proc/self/status | tr -s "\t\n" "  "')"
# exec: the service replaces this root shell as PID 1 (gets Railway's stop signal; no root process stays behind).
export HOME=/home/$APP USER=$APP LOGNAME=$APP
exec setpriv --reuid="$APP" --regid="$APP" --init-groups --inh-caps=-all -- "$@"
