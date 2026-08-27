#!/usr/bin/env bash
set -Eeuo pipefail
umask 077

ROOT="$(pwd -P)"
TEST_ROOT="$(mktemp -d /tmp/pipewire-installer-test.XXXXXX)"
trap 'rm -rf -- "$TEST_ROOT"' EXIT
mkdir -p "$TEST_ROOT/bin" "$TEST_ROOT/home"

cat >"$TEST_ROOT/bin/curl" <<'CURL'
#!/usr/bin/env bash
set -Eeuo pipefail
output=''
write_out=''
url=''
while (($#)); do
    case "$1" in
        --output) output="$2"; shift 2 ;;
        --write-out) write_out="$2"; shift 2 ;;
        --proto|--tlsv1.2) if [[ "$1" = --proto ]]; then shift 2; else shift; fi ;;
        --fail|--silent|--show-error|--location) shift ;;
        *) url="$1"; shift ;;
    esac
done
payload='#!/usr/bin/env bash
set -Eeuo pipefail
if [[ "${1:-}" = --version ]]; then
  printf "%s\n" "PipeWire App Launcher 9.8.7"
elif [[ "${1:-}" = --appimage-extract ]]; then
  path="squashfs-root/usr/share/icons/hicolor/scalable/apps"
  mkdir -p "$path"
  printf "%s\n" "<svg/>" >"$path/pipewire-app-launcher.svg"
fi
'
case "$url" in
    */releases/latest)
        [[ "$output" = /dev/null ]]
        printf '%s' 'https://github.com/triplora/PipeWire-App-Launcher/releases/tag/v9.8.7'
        ;;
    */PipeWire-App-Launcher-x86_64.AppImage.sha256)
        digest="$(printf '%s' "$payload" | sha256sum | awk '{print $1}')"
        printf '%s  %s\n' "$digest" 'PipeWire-App-Launcher-x86_64.AppImage' >"$output"
        ;;
    */PipeWire-App-Launcher-x86_64.AppImage)
        printf '%s' "$payload" >"$output"
        ;;
    *) exit 22 ;;
esac
CURL
chmod 700 "$TEST_ROOT/bin/curl"

export HOME="$TEST_ROOT/home"
export XDG_DATA_HOME="$TEST_ROOT/home/data"
export XDG_BIN_HOME="$TEST_ROOT/home/bin"
export PATH="$TEST_ROOT/bin:/usr/bin:/bin"

set +e
bash "$ROOT/scripts/install-or-update.sh" --check >"$TEST_ROOT/check-missing.log" 2>&1
check_missing_exit=$?
set -e
[[ "$check_missing_exit" -eq 10 ]]
grep -q '^INSTALL_OR_UPDATE_CHECK=NOT_INSTALLED$' "$TEST_ROOT/check-missing.log"
[[ ! -e "$XDG_DATA_HOME" ]]

bash "$ROOT/scripts/install-or-update.sh" >"$TEST_ROOT/install.log" 2>&1
grep -q '^ACTION=INSTALLED$' "$TEST_ROOT/install.log"
grep -q '^INSTALL_OR_UPDATE_RESULT=PASS$' "$TEST_ROOT/install.log"
[[ -x "$XDG_DATA_HOME/pipewire-app-launcher/PipeWire-App-Launcher-x86_64.AppImage" ]]
[[ -L "$XDG_BIN_HOME/pipewire-app-launcher" ]]
[[ -f "$XDG_DATA_HOME/applications/pipewire-app-launcher.desktop" ]]
[[ -f "$XDG_DATA_HOME/icons/hicolor/scalable/apps/pipewire-app-launcher.svg" ]]

bash "$ROOT/scripts/install-or-update.sh" --check >"$TEST_ROOT/check-current.log" 2>&1
grep -q '^INSTALL_OR_UPDATE_CHECK=CURRENT$' "$TEST_ROOT/check-current.log"

printf '%s\n' 'outdated-appimage' >"$XDG_DATA_HOME/pipewire-app-launcher/PipeWire-App-Launcher-x86_64.AppImage"
set +e
bash "$ROOT/scripts/install-or-update.sh" --check >"$TEST_ROOT/check-update.log" 2>&1
check_update_exit=$?
set -e
[[ "$check_update_exit" -eq 10 ]]
grep -q '^INSTALL_OR_UPDATE_CHECK=UPDATE_AVAILABLE$' "$TEST_ROOT/check-update.log"

bash "$ROOT/scripts/install-or-update.sh" >"$TEST_ROOT/update.log" 2>&1
grep -q '^ACTION=UPDATED$' "$TEST_ROOT/update.log"
grep -q '^INSTALL_OR_UPDATE_RESULT=PASS$' "$TEST_ROOT/update.log"
grep -q '^outdated-appimage$' \
    "$XDG_DATA_HOME/pipewire-app-launcher/PipeWire-App-Launcher-x86_64.AppImage.previous"

bash "$ROOT/scripts/install-or-update.sh" --force >"$TEST_ROOT/force.log" 2>&1
grep -q '^ACTION=REINSTALLED$' "$TEST_ROOT/force.log"
grep -q '^INSTALL_OR_UPDATE_RESULT=PASS$' "$TEST_ROOT/force.log"
[[ -f "$XDG_DATA_HOME/pipewire-app-launcher/PipeWire-App-Launcher-x86_64.AppImage.previous" ]]

printf '%s\n' \
  'CHECK_NOT_INSTALLED=PASS' \
  'INSTALL=PASS' \
  'CHECK_CURRENT=PASS' \
  'CHECK_UPDATE_AVAILABLE=PASS' \
  'UPDATE=PASS' \
  'FORCE_REINSTALL=PASS' \
  'OFFLINE_INSTALLER_MATRIX=PASS'
