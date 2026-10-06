#!/usr/bin/env bash
# Installs an application-menu launcher (and icon) for tools/treadmill_app.py
# for the current user -- it appears in the XFCE Whisker menu under
# Utilities. The .desktop file needs absolute paths to this checkout and its
# .venv, so it's generated here rather than checked in; re-run this after
# moving the repo. Remove with:
#   rm ~/.local/share/applications/treadmill-app.desktop \
#      ~/.local/share/icons/hicolor/scalable/apps/treadmill-app.svg
set -euo pipefail

repo="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
python="$repo/.venv/bin/python3"
if [ ! -x "$python" ]; then
    echo "No venv at $repo/.venv -- create it first (see README: it needs" >&2
    echo "--system-site-packages for the tray icon's gi bindings)." >&2
    exit 1
fi

data="${XDG_DATA_HOME:-$HOME/.local/share}"
mkdir -p "$data/applications" "$data/icons/hicolor/scalable/apps"
cp "$repo/tools/treadmill-app.svg" "$data/icons/hicolor/scalable/apps/treadmill-app.svg"

cat > "$data/applications/treadmill-app.desktop" <<EOF
[Desktop Entry]
Type=Application
Name=Treadmill
GenericName=Treadmill controller
Comment=Control the treadmill over Bluetooth and view step history
Exec="$python" "$repo/tools/treadmill_app.py"
Path=$repo/tools
Icon=treadmill-app
Terminal=false
Categories=Utility;
Keywords=treadmill;steps;walking;
EOF

update-desktop-database "$data/applications" 2>/dev/null || true
gtk-update-icon-cache -f -t "$data/icons/hicolor" 2>/dev/null || true
echo "Installed: $data/applications/treadmill-app.desktop"
