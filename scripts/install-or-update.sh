#!/usr/bin/env bash
set -Eeuo pipefail
set +x
umask 077

readonly REPOSITORY='triplora/PipeWire-App-Launcher'
readonly PRODUCT='PipeWire App Launcher'
readonly ASSET='PipeWire-App-Launcher-x86_64.AppImage'
readonly CHECKSUM_ASSET="${ASSET}.sha256"
readonly RELEASES_URL="https://github.com/${REPOSITORY}/releases"

CHECK_ONLY=0
FORCE=0
TEMP_DIR=''

usage() {
    cat <<'EOF'
Usage: install-or-update.sh [--check] [--force]

Install or update the latest official PipeWire App Launcher AppImage for the
current user. No sudo privileges are used.

Options:
  --check   Check whether installation or an update is needed; change nothing.
            Exit 0 means current, exit 10 means install/update is available.
  --force   Reinstall the latest version even when its checksum is current.
  -h        Show this help.
EOF
}

fail() {
    printf 'ERROR=%s\n' "$1" >&2
    printf 'INSTALL_OR_UPDATE_RESULT=FAIL\n' >&2
    exit 1
}

cleanup() {
    if [[ -n "$TEMP_DIR" && -d "$TEMP_DIR" && ! -L "$TEMP_DIR" ]]; then
        case "$TEMP_DIR" in
            "${TMPDIR:-/tmp}"/pipewire-app-launcher-update.*) rm -rf -- "$TEMP_DIR" ;;
        esac
    fi
}
trap cleanup EXIT

while (($#)); do
    case "$1" in
        --check) CHECK_ONLY=1 ;;
        --force) FORCE=1 ;;
        -h|--help) usage; exit 0 ;;
        *) usage >&2; fail UNKNOWN_OPTION ;;
    esac
    shift
done

[[ "$CHECK_ONLY" -eq 0 || "$FORCE" -eq 0 ]] || fail CHECK_AND_FORCE_CONFLICT
[[ "$(uname -m)" = x86_64 ]] || fail UNSUPPORTED_ARCHITECTURE

for command_name in curl sha256sum stat mktemp install mv cp ln dirname basename \
    sed awk chmod mkdir readlink id uname; do
    command -v "$command_name" >/dev/null 2>&1 || fail "MISSING_COMMAND_${command_name^^}"
done

readonly DATA_HOME="${XDG_DATA_HOME:-$HOME/.local/share}"
readonly BIN_HOME="${XDG_BIN_HOME:-$HOME/.local/bin}"
readonly APP_DIR="$DATA_HOME/pipewire-app-launcher"
readonly APPIMAGE_PATH="$APP_DIR/$ASSET"
readonly PREVIOUS_PATH="$APP_DIR/${ASSET}.previous"
readonly BIN_PATH="$BIN_HOME/pipewire-app-launcher"
readonly APPLICATIONS_DIR="$DATA_HOME/applications"
readonly DESKTOP_PATH="$APPLICATIONS_DIR/pipewire-app-launcher.desktop"
readonly ICON_DIR="$DATA_HOME/icons/hicolor/scalable/apps"
readonly ICON_PATH="$ICON_DIR/pipewire-app-launcher.svg"

printf '%s\n' '=== PIPEWIRE APP LAUNCHER INSTALL OR UPDATE ==='
printf 'REPOSITORY=%s\n' "$REPOSITORY"
printf 'INSTALL_PATH=%s\n' "$APPIMAGE_PATH"
printf 'COMMAND_PATH=%s\n' "$BIN_PATH"

LATEST_URL="$(
    curl --fail --silent --show-error --location \
        --proto '=https' --tlsv1.2 \
        --output /dev/null --write-out '%{url_effective}' \
        "$RELEASES_URL/latest"
)" || fail LATEST_RELEASE_LOOKUP_FAILED

LATEST_TAG="${LATEST_URL%/}"
LATEST_TAG="${LATEST_TAG##*/}"
[[ "$LATEST_TAG" =~ ^v([0-9]+\.[0-9]+\.[0-9]+)$ ]] || fail INVALID_LATEST_TAG
readonly LATEST_VERSION="${BASH_REMATCH[1]}"
readonly DOWNLOAD_BASE="$RELEASES_URL/download/$LATEST_TAG"

printf 'LATEST_TAG=%s\n' "$LATEST_TAG"
printf 'LATEST_VERSION=%s\n' "$LATEST_VERSION"

if [[ -e "$APP_DIR" || -L "$APP_DIR" ]]; then
    [[ -d "$APP_DIR" && ! -L "$APP_DIR" ]] || fail UNSAFE_APPLICATION_DIRECTORY
    [[ "$(stat -c '%u' "$APP_DIR")" = "$(id -u)" ]] || fail APPLICATION_DIRECTORY_NOT_OWNED
fi

TEMP_DIR="$(mktemp -d "${TMPDIR:-/tmp}/pipewire-app-launcher-update.XXXXXX")"
readonly DOWNLOAD_APPIMAGE="$TEMP_DIR/$ASSET"
readonly DOWNLOAD_CHECKSUM="$TEMP_DIR/$CHECKSUM_ASSET"

curl --fail --silent --show-error --location \
    --proto '=https' --tlsv1.2 \
    --output "$DOWNLOAD_CHECKSUM" \
    "$DOWNLOAD_BASE/$CHECKSUM_ASSET" || fail CHECKSUM_DOWNLOAD_FAILED

mapfile -t CHECKSUM_LINES < <(sed '/^[[:space:]]*$/d; s/\r$//' "$DOWNLOAD_CHECKSUM")
[[ "${#CHECKSUM_LINES[@]}" -eq 1 ]] || fail INVALID_CHECKSUM_FILE
CHECKSUM_LINE="${CHECKSUM_LINES[0]}"
ASSET_REGEX="${ASSET//./\\.}"
[[ "$CHECKSUM_LINE" =~ ^([0-9A-Fa-f]{64})[[:space:]]+${ASSET_REGEX}$ ]] || fail INVALID_CHECKSUM_CONTRACT
readonly EXPECTED_SHA256="${BASH_REMATCH[1],,}"

INSTALLED_STATE='NOT_INSTALLED'
INSTALLED_SHA256=''
if [[ -e "$APPIMAGE_PATH" || -L "$APPIMAGE_PATH" ]]; then
    [[ -f "$APPIMAGE_PATH" && ! -L "$APPIMAGE_PATH" ]] || fail UNSAFE_INSTALLED_APPIMAGE
    [[ "$(stat -c '%u' "$APPIMAGE_PATH")" = "$(id -u)" ]] || fail INSTALLED_APPIMAGE_NOT_OWNED
    INSTALLED_SHA256="$(sha256sum "$APPIMAGE_PATH" | awk '{print $1}')"
    if [[ "$INSTALLED_SHA256" = "$EXPECTED_SHA256" ]]; then
        INSTALLED_STATE='CURRENT'
    else
        INSTALLED_STATE='UPDATE_AVAILABLE'
    fi
fi

printf 'INSTALLATION_STATE=%s\n' "$INSTALLED_STATE"
printf 'EXPECTED_SHA256=%s\n' "$EXPECTED_SHA256"
if [[ -n "$INSTALLED_SHA256" ]]; then printf 'INSTALLED_SHA256=%s\n' "$INSTALLED_SHA256"; fi

if [[ "$CHECK_ONLY" -eq 1 ]]; then
    if [[ "$INSTALLED_STATE" = CURRENT ]]; then
        printf '%s\n' 'ACTION_REQUIRED=NO' 'INSTALL_OR_UPDATE_CHECK=CURRENT'
        exit 0
    fi
    printf '%s\n' 'ACTION_REQUIRED=YES' "INSTALL_OR_UPDATE_CHECK=$INSTALLED_STATE"
    exit 10
fi

if [[ "$INSTALLED_STATE" = CURRENT && "$FORCE" -eq 0 ]]; then
    printf '%s\n' 'ACTION=NONE' 'INSTALL_OR_UPDATE_RESULT=CURRENT'
    exit 0
fi

mkdir -p -- "$APP_DIR"
[[ -d "$APP_DIR" && ! -L "$APP_DIR" ]] || fail UNSAFE_APPLICATION_DIRECTORY
[[ "$(stat -c '%u' "$APP_DIR")" = "$(id -u)" ]] || fail APPLICATION_DIRECTORY_NOT_OWNED

curl --fail --silent --show-error --location \
    --proto '=https' --tlsv1.2 \
    --output "$DOWNLOAD_APPIMAGE" \
    "$DOWNLOAD_BASE/$ASSET" || fail APPIMAGE_DOWNLOAD_FAILED

[[ -f "$DOWNLOAD_APPIMAGE" && ! -L "$DOWNLOAD_APPIMAGE" && -s "$DOWNLOAD_APPIMAGE" ]] || fail DOWNLOADED_APPIMAGE_INVALID
ACTUAL_SHA256="$(sha256sum "$DOWNLOAD_APPIMAGE" | awk '{print $1}')"
[[ "$ACTUAL_SHA256" = "$EXPECTED_SHA256" ]] || fail APPIMAGE_SHA256_MISMATCH
chmod 700 "$DOWNLOAD_APPIMAGE"

DOWNLOADED_VERSION="$(APPIMAGE_EXTRACT_AND_RUN=1 "$DOWNLOAD_APPIMAGE" --version)" || fail APPIMAGE_VERSION_CHECK_FAILED
[[ "$DOWNLOADED_VERSION" = "$PRODUCT $LATEST_VERSION" ]] || fail APPIMAGE_VERSION_MISMATCH
printf 'DOWNLOADED_VERSION=%s\n' "$DOWNLOADED_VERSION"
printf '%s\n' 'DOWNLOADED_APPIMAGE_VALIDATION=PASS'

mkdir -p -- "$BIN_HOME" "$APPLICATIONS_DIR" "$ICON_DIR"
for directory in "$BIN_HOME" "$APPLICATIONS_DIR" "$ICON_DIR"; do
    [[ -d "$directory" && ! -L "$directory" ]] || fail UNSAFE_XDG_DIRECTORY
    [[ "$(stat -c '%u' "$directory")" = "$(id -u)" ]] || fail XDG_DIRECTORY_NOT_OWNED
done

if [[ -e "$BIN_PATH" || -L "$BIN_PATH" ]]; then
    if [[ ! -L "$BIN_PATH" || "$(readlink -f "$BIN_PATH")" != "$APPIMAGE_PATH" ]]; then
        fail COMMAND_PATH_COLLISION
    fi
fi
for managed_file in "$PREVIOUS_PATH" "$DESKTOP_PATH" "$ICON_PATH"; do
    if [[ -L "$managed_file" ]]; then fail MANAGED_PATH_IS_SYMLINK; fi
    if [[ -e "$managed_file" && "$(stat -c '%u' "$managed_file")" != "$(id -u)" ]]; then
        fail MANAGED_PATH_NOT_OWNED
    fi
done

EXTRACT_DIR="$TEMP_DIR/extract"
mkdir -p "$EXTRACT_DIR"
(
    cd "$EXTRACT_DIR"
    "$DOWNLOAD_APPIMAGE" --appimage-extract \
        'usr/share/icons/hicolor/scalable/apps/pipewire-app-launcher.svg' \
        >/dev/null
) || fail ICON_EXTRACTION_FAILED
EXTRACTED_ICON="$EXTRACT_DIR/squashfs-root/usr/share/icons/hicolor/scalable/apps/pipewire-app-launcher.svg"
[[ -f "$EXTRACTED_ICON" && ! -L "$EXTRACTED_ICON" && -s "$EXTRACTED_ICON" ]] || fail EXTRACTED_ICON_INVALID

cat >"$TEMP_DIR/pipewire-app-launcher.desktop" <<EOF
[Desktop Entry]
Type=Application
Name=PipeWire App Launcher
Comment=Launch JACK applications through PipeWire
Exec=$BIN_PATH
Icon=pipewire-app-launcher
Categories=AudioVideo;Audio;Utility;
Terminal=false
EOF

if [[ -f "$APPIMAGE_PATH" && ! -L "$APPIMAGE_PATH" ]]; then
    cp --preserve=mode,timestamps -- "$APPIMAGE_PATH" "$TEMP_DIR/.previous-appimage"
    mv -f -- "$TEMP_DIR/.previous-appimage" "$PREVIOUS_PATH"
fi

install -m 0755 -- "$DOWNLOAD_APPIMAGE" "$TEMP_DIR/.new-appimage"
mv -f -- "$TEMP_DIR/.new-appimage" "$APPIMAGE_PATH"
ln -sfn -- "$APPIMAGE_PATH" "$BIN_PATH"
install -m 0644 -- "$EXTRACTED_ICON" "$ICON_PATH"
install -m 0644 -- "$TEMP_DIR/pipewire-app-launcher.desktop" "$DESKTOP_PATH"

if command -v update-desktop-database >/dev/null 2>&1; then
    update-desktop-database "$APPLICATIONS_DIR" >/dev/null 2>&1 || true
fi

FINAL_SHA256="$(sha256sum "$APPIMAGE_PATH" | awk '{print $1}')"
FINAL_VERSION="$(APPIMAGE_EXTRACT_AND_RUN=1 "$APPIMAGE_PATH" --version)" || fail INSTALLED_VERSION_CHECK_FAILED
[[ "$FINAL_SHA256" = "$EXPECTED_SHA256" ]] || fail INSTALLED_SHA256_MISMATCH
[[ "$FINAL_VERSION" = "$PRODUCT $LATEST_VERSION" ]] || fail INSTALLED_VERSION_MISMATCH
[[ -L "$BIN_PATH" && "$(readlink -f "$BIN_PATH")" = "$APPIMAGE_PATH" ]] || fail COMMAND_LINK_MISMATCH
[[ -f "$DESKTOP_PATH" && -f "$ICON_PATH" ]] || fail DESKTOP_INTEGRATION_MISSING

if [[ "$INSTALLED_STATE" = NOT_INSTALLED ]]; then ACTION='INSTALLED';
elif [[ "$FORCE" -eq 1 ]]; then ACTION='REINSTALLED';
else ACTION='UPDATED'; fi

printf '%s\n' '=== FINAL STATE ==='
printf 'ACTION=%s\n' "$ACTION"
printf 'INSTALLED_VERSION=%s\n' "$FINAL_VERSION"
printf 'INSTALLED_SHA256=%s\n' "$FINAL_SHA256"
printf 'APPIMAGE_PATH=%s\n' "$APPIMAGE_PATH"
printf 'COMMAND_PATH=%s\n' "$BIN_PATH"
printf 'DESKTOP_PATH=%s\n' "$DESKTOP_PATH"
printf 'PREVIOUS_PATH=%s\n' "$([[ -f "$PREVIOUS_PATH" ]] && printf '%s' "$PREVIOUS_PATH" || printf NONE)"
printf '%s\n' 'INSTALL_OR_UPDATE_RESULT=PASS'
