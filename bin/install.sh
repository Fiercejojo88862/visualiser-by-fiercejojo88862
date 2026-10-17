#!/usr/bin/env bash
# Installs the launcher, desktop entry, icon and systemd user unit, then wires an
# Omarchy/Hyprland keybind. Symlinks point back into this folder so the app stays
# the single source of truth (the folder itself stays where it is - in ~/Music).
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "$(readlink -f -- "${BASH_SOURCE[0]}")")" && pwd)"
APP_DIR="$(cd -- "$SCRIPT_DIR/.." && pwd)"
UNIT_NAME="visualiser-by-fiercejojo88862.service"

hdr() { printf '\n== %s\n' "$*"; }

hdr "launcher"
mkdir -p "$HOME/.local/bin"
ln -sfn "$APP_DIR/bin/visualiser" "$HOME/.local/bin/visualiser"
chmod +x "$APP_DIR/bin/visualiser" "$APP_DIR/bin/run-container.sh" "$APP_DIR/bin/install.sh"
echo "~/.local/bin/visualiser -> $APP_DIR/bin/visualiser"

hdr "desktop entry + icon"
mkdir -p "$HOME/.local/share/applications"
install -m 644 "$APP_DIR/desktop/visualiser.desktop" "$HOME/.local/share/applications/visualiser.desktop"
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
ln -sfn "$APP_DIR/systemd/$UNIT_NAME" "$HOME/.config/systemd/user/$UNIT_NAME"
systemctl --user daemon-reload
echo "linked $UNIT_NAME (deliberately not enabled at login)"

hdr "done"
cat <<EOF
Start it with:
    visualiser            # or: visualiser start
Stop it with:
    visualiser stop
Check it with:
    visualiser status
EOF
