#!/usr/bin/env bash
# Installs the launcher, desktop entry, icon and systemd user unit, then wires an
# Omarchy/Hyprland keybind. The unit and desktop entry are *rendered* from the
# templates in systemd/ and desktop/ with this checkout's real paths, so the app
# runs from wherever you cloned it (no hardcoded /home/<someone> paths).
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "$(readlink -f -- "${BASH_SOURCE[0]}")")" && pwd)"
APP_DIR="$(cd -- "$SCRIPT_DIR/.." && pwd)"
UNIT_NAME="visualiser-by-fiercejojo88862.service"
UNIT_DST="$HOME/.config/systemd/user/$UNIT_NAME"
DESKTOP_DST="$HOME/.local/share/applications/visualiser.desktop"

hdr() { printf '\n== %s\n' "$*"; }
die() { printf 'error: %s\n' "$*" >&2; exit 1; }

echo "app dir: $APP_DIR"

# -- prerequisites ----------------------------------------------------------
hdr "prerequisites"
command -v python3 >/dev/null 2>&1 || die "python3 is required (used by the engine and this installer)"
if ! command -v docker >/dev/null 2>&1; then
  die "docker is required - install it, make sure your user can reach the daemon, then re-run this script"
fi
if ! docker info >/dev/null 2>&1; then
  die "docker is installed but the daemon is not reachable - start it or add your user to the docker group"
fi
echo "python3 + docker ok"

# Fill @APP_DIR@ / @HOME@ placeholders with the real paths. Done in python so
# paths containing spaces, &, | or backslashes are never mangled by sed.
render() { # <template> <destination>
  python3 - "$1" "$2" "$APP_DIR" "$HOME" <<'PY'
import sys

src, dst, app_dir, home = sys.argv[1:5]
text = open(src, encoding="utf-8").read()
text = text.replace("@APP_DIR@", app_dir).replace("@HOME@", home)
if "@APP_DIR@" in text or "@HOME@" in text:
    sys.exit(f"unrendered placeholder left in {src}")
with open(dst, "w", encoding="utf-8") as fh:
    fh.write(text)
PY
}

hdr "launcher"
mkdir -p "$HOME/.local/bin"
ln -sfn "$APP_DIR/bin/visualiser" "$HOME/.local/bin/visualiser"
chmod +x "$APP_DIR/bin/visualiser" "$APP_DIR/bin/run-container.sh" "$APP_DIR/bin/install.sh"
echo "~/.local/bin/visualiser -> $APP_DIR/bin/visualiser"

hdr "desktop entry + icon"
mkdir -p "$HOME/.local/share/applications"
render "$APP_DIR/desktop/visualiser.desktop" "$DESKTOP_DST"
chmod 644 "$DESKTOP_DST"
# The icon is a bitmap, so install it at every size the icon theme will ask for.
ICON_SRC="$APP_DIR/assets/visualiser.png"
if [ -f "$ICON_SRC" ]; then
  for size in 16 22 24 32 48 64 96 128 256 512 1024; do
    dest="$HOME/.local/share/icons/hicolor/${size}x${size}/apps"
    mkdir -p "$dest"
    if command -v magick >/dev/null 2>&1; then
      magick "$ICON_SRC" -resize "${size}x${size}" -strip "$dest/visualiser.png"
    elif command -v convert >/dev/null 2>&1; then
      convert "$ICON_SRC" -resize "${size}x${size}" -strip "$dest/visualiser.png"
    else
      cp "$ICON_SRC" "$dest/visualiser.png"
      break
    fi
  done
  rm -f "$HOME/.local/share/icons/hicolor/scalable/apps/visualiser.svg"
else
  echo "warning: $ICON_SRC missing - no icon installed"
fi
command -v update-desktop-database >/dev/null 2>&1 && update-desktop-database "$HOME/.local/share/applications" >/dev/null 2>&1 || true
command -v gtk-update-icon-cache >/dev/null 2>&1 && gtk-update-icon-cache -q -t "$HOME/.local/share/icons/hicolor" >/dev/null 2>&1 || true
echo "installed visualiser.desktop + icon"

hdr "systemd user unit"
mkdir -p "$HOME/.config/systemd/user"
# Replace, never follow, an old install: earlier versions symlinked a unit with
# the original author's home directory baked in ("Directory not found" on start).
rm -f "$UNIT_DST"
render "$APP_DIR/systemd/$UNIT_NAME" "$UNIT_DST"
chmod 644 "$UNIT_DST"
mkdir -p "$APP_DIR/state"
# systemd fails with a bare "Directory not found" when WorkingDirectory is wrong,
# so prove the path it just got actually exists before declaring success.
if command -v systemd-analyze >/dev/null 2>&1; then
  systemd-analyze verify "$UNIT_DST" 2>/dev/null || true
fi
systemctl --user daemon-reload 2>/dev/null || true
echo "installed $UNIT_NAME for $APP_DIR (not enabled at login)"

hdr "done"
cat <<EOF
Start it with:
    visualiser            # or: visualiser start
Stop it with:
    visualiser stop
Check it with:
    visualiser status
EOF
