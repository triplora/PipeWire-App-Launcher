#!/usr/bin/env bash
set -euo pipefail

project_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$project_root"

xcb_cursor_lib="$(ldconfig -p 2>/dev/null | awk '/libxcb-cursor\.so\.0 / { print $NF; exit }')"
if [[ -z "$xcb_cursor_lib" || ! -f "$xcb_cursor_lib" ]]; then
  cat >&2 <<'EOF'
ERROR: libxcb-cursor.so.0 was not found on the build host.

Install the Ubuntu build dependency and run this script again:
  sudo apt update
  sudo apt install libxcb-cursor0

The library is copied into the AppImage, so users of the resulting AppImage
do not need to install this package separately.
EOF
  exit 1
fi

python3 -m venv .build-venv
. .build-venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
python -m PyInstaller --noconfirm --clean --windowed --paths "$project_root" --name pipewire-app-launcher --icon assets/pipewire-app-launcher.svg pipewire_launcher/__main__.py

appdir="$project_root/build/AppDir"
rm -rf "$appdir"
mkdir -p "$appdir/usr/bin" "$appdir/usr/lib" "$appdir/usr/share/applications" "$appdir/usr/share/icons/hicolor/scalable/apps"
cp -a dist/pipewire-app-launcher/. "$appdir/usr/bin/"
cp -L "$xcb_cursor_lib" "$appdir/usr/lib/libxcb-cursor.so.0"
cp assets/pipewire-app-launcher.desktop "$appdir/usr/share/applications/"
cp assets/pipewire-app-launcher.svg "$appdir/usr/share/icons/hicolor/scalable/apps/"
ln -s usr/share/applications/pipewire-app-launcher.desktop "$appdir/pipewire-app-launcher.desktop"
ln -s usr/share/icons/hicolor/scalable/apps/pipewire-app-launcher.svg "$appdir/pipewire-app-launcher.svg"

printf '%s\n' \
  '#!/bin/sh' \
  'export LD_LIBRARY_PATH="$APPDIR/usr/lib${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"' \
  'exec "$APPDIR/usr/bin/pipewire-app-launcher" "$@"' > "$appdir/AppRun"
chmod +x "$appdir/AppRun"

if ! LD_LIBRARY_PATH="$appdir/usr/lib${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}" \
  ldd "$appdir/usr/bin/_internal/PySide6/Qt/plugins/platforms/libqxcb.so" \
  | awk '/not found/ { missing=1; print > "/dev/stderr" } END { exit missing }'; then
  echo "ERROR: unresolved Qt xcb runtime libraries remain in AppDir." >&2
  exit 1
fi

tool="$project_root/build/appimagetool-x86_64.AppImage"
if [[ ! -x "$tool" ]]; then
  curl -fL -o "$tool" https://github.com/AppImage/appimagetool/releases/download/continuous/appimagetool-x86_64.AppImage
  chmod +x "$tool"
fi
mkdir -p dist
ARCH=x86_64 "$tool" "$appdir" "dist/PipeWire-App-Launcher-x86_64.AppImage"
echo "Created dist/PipeWire-App-Launcher-x86_64.AppImage"
