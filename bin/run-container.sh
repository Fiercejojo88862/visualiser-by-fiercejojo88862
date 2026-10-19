#!/usr/bin/env bash
# Runs the visualiser container in the foreground.
#
# This is the lifecycle contract: systemd treats this process as the unit, the
# container exits when the Qt window closes (or on SIGTERM from `systemctl stop`),
# and docker removes it via --rm. No polling, no stale containers, nothing left
# running behind.
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "$(readlink -f -- "${BASH_SOURCE[0]}")")" && pwd)"
APP_DIR="$(cd -- "$SCRIPT_DIR/.." && pwd)"

IMAGE="${VISUALISER_IMAGE:-visualiser-by-fiercejojo88862:local}"
NAME="visualiser-by-fiercejojo88862"
UID_NUM="$(id -u)"
GID_NUM="$(id -g)"
XDG="${XDG_RUNTIME_DIR:-/run/user/${UID_NUM}}"

if ! docker image inspect "$IMAGE" >/dev/null 2>&1; then
  echo "image $IMAGE missing - building it now"
  docker build -f "$APP_DIR/docker/Dockerfile" -t "$IMAGE" "$APP_DIR"
fi

# Refuse to double-start: two engines would fight over the port and the API cache.
if docker ps --format '{{.Names}}' | grep -qx "$NAME"; then
  echo "already running (container $NAME)"
  exit 0
fi

DOCKER_ARGS=(
  --rm
  --name "$NAME"
  --stop-timeout 5
  --user "${UID_NUM}:${GID_NUM}"
  --ipc host
  --shm-size 2g
  --device /dev/dri
  --workdir /app
  -e "XDG_RUNTIME_DIR=${XDG}"
  -e "VISUALISER_APP_DIR=/app"
  -e "VISUALISER_STATE_DIR=/app/state"
  -e "PULSE_SERVER=unix:${XDG}/pulse/native"
  -e "DBUS_SESSION_BUS_ADDRESS=unix:path=${XDG}/bus"
  -e "HOME=/tmp"
  -e "XDG_CACHE_HOME=/tmp/qtcache"
  -e "XDG_CONFIG_HOME=/tmp/qtconfig"
  -e "XDG_DATA_HOME=/tmp/qtdat"
  -v "${XDG}:${XDG}"
  -v "${APP_DIR}:/app"
)

# A local clip folder lives outside the app dir, so bind it in at the *same*
# absolute path - that keeps settings.json portable between host and container.
LIB="$(APP_DIR="$APP_DIR" python3 - <<'PY' 2>/dev/null || true
import json, os
path = os.path.join(os.environ["APP_DIR"], "state", "settings.json")
try:
    print((json.load(open(path)).get("local_library") or "").strip())
except Exception:
    print("")
PY
)"
LIB="${LIB/#\~/$HOME}"
if [ -n "$LIB" ] && [ -d "$LIB" ]; then
  DOCKER_ARGS+=(-v "${LIB}:${LIB}:ro")
  echo "local clip library mounted: $LIB"
elif [ -n "$LIB" ]; then
  echo "warning: local_library '$LIB' is not a directory on this host" >&2
fi

# Firefox writes MPRIS artwork into ~/.config/mozilla/firefox/firefox-mpris/.
# That is where mpris:artUrl points for web players (Apple Music, Spotify web),
# so mount it read-only at the same path or the album art is invisible in here.
FIREFOX_ART="${HOME}/.config/mozilla/firefox/firefox-mpris"
if [ -d "$FIREFOX_ART" ]; then
  DOCKER_ARGS+=(-v "${FIREFOX_ART}:${FIREFOX_ART}:ro")
fi

# Any other player that drops artwork in a temp path can be mounted the same way
# through VISUALISER_EXTRA_MOUNTS="/path/a:/path/a:ro /path/b:/path/b:ro".
for m in ${VISUALISER_EXTRA_MOUNTS:-}; do
  DOCKER_ARGS+=(-v "$m")
done

# Wayland only if the session actually has it; otherwise fall back to X11/XWayland.
if [ -n "${WAYLAND_DISPLAY:-}" ] && [ -S "${XDG}/${WAYLAND_DISPLAY}" ]; then
  DOCKER_ARGS+=(-e "WAYLAND_DISPLAY=${WAYLAND_DISPLAY}" -e "QT_QPA_PLATFORM=wayland")
elif [ -n "${DISPLAY:-}" ]; then
  DOCKER_ARGS+=(-e "DISPLAY=${DISPLAY}" -e "QT_QPA_PLATFORM=xcb" -v /tmp/.X11-unix:/tmp/.X11-unix)
else
  echo "warning: no Wayland or X11 display found in this environment" >&2
fi

exec docker run "${DOCKER_ARGS[@]}" "$IMAGE" python -m app "$@"
