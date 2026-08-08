#!/usr/bin/env bash
set -Eeuo pipefail

project_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"
script_start_epoch="$(date +%s)"
work_dir=""
output_dir=""
appimagetool=""
appimagetool_sha256=""
runtime_file=""
runtime_sha256=""
work_dir_given=0
output_dir_given=0
work_dir_created_by_script=0
output_dir_created_by_script=0
execution_dir=""

usage() {
  cat <<'EOF'
Usage: scripts/build-appimage.sh --appimagetool FILE --appimagetool-sha256 SHA256 [OPTIONS]

Options:
  --work-dir DIR             Existing, non-symlink directory for this build.
  --output-dir DIR           Directory where the new AppImage is preserved.
  --appimagetool FILE        Locally supplied, validated appimagetool binary.
  --appimagetool-sha256 HEX  Expected SHA-256 for FILE (required).
  --runtime-file FILE        Locally supplied, validated x86_64 type-2 runtime.
  --runtime-sha256 HEX       Expected SHA-256 for runtime (required).
  -h, --help                 Show this help.

Without --work-dir or --output-dir, temporary directories are created with
mktemp. The temporary output is preserved and its absolute path is printed.
EOF
}

die() { echo "ERROR: $*" >&2; exit 1; }

while (($#)); do
  case "$1" in
    --work-dir) (($# >= 2)) || die "--work-dir requires a directory"; work_dir="$2"; work_dir_given=1; shift 2 ;;
    --output-dir) (($# >= 2)) || die "--output-dir requires a directory"; output_dir="$2"; output_dir_given=1; shift 2 ;;
    --appimagetool) (($# >= 2)) || die "--appimagetool requires a file"; appimagetool="$2"; shift 2 ;;
    --appimagetool-sha256) (($# >= 2)) || die "--appimagetool-sha256 requires a digest"; appimagetool_sha256="$2"; shift 2 ;;
    --runtime-file) (($# >= 2)) || die "--runtime-file requires a file"; runtime_file="$2"; shift 2 ;;
    --runtime-sha256) (($# >= 2)) || die "--runtime-sha256 requires a digest"; runtime_sha256="$2"; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    *) die "unknown option: $1" ;;
  esac
done

machine="$(uname -m)"
[[ "$machine" == "x86_64" ]] || die "unsupported architecture: $machine; only x86_64 is supported"
[[ -n "$appimagetool" ]] || die "--appimagetool is required; automatic downloads are disabled"
[[ -n "$appimagetool_sha256" && "$appimagetool_sha256" =~ ^[[:xdigit:]]{64}$ ]] || die "--appimagetool-sha256 must be a 64-character hexadecimal digest"
[[ -n "$runtime_file" ]] || die "--runtime-file is required; automatic runtime downloads are disabled"
[[ -n "$runtime_sha256" && "$runtime_sha256" =~ ^[[:xdigit:]]{64}$ ]] || die "--runtime-sha256 must be a 64-character hexadecimal digest"

validate_base_dir() {
  local label="$1" candidate="$2" canonical parent
  [[ -n "$candidate" ]] || die "$label must not be empty"
  [[ "$candidate" != "/" ]] || die "$label must not be /"
  [[ "$candidate" != "$HOME" ]] || die "$label must not be HOME"
  [[ "$candidate" != "$project_root" ]] || die "$label must not be the checkout"
  [[ ! -L "$candidate" ]] || die "$label must not be a symbolic link: $candidate"
  if [[ -e "$candidate" ]]; then
    [[ -d "$candidate" ]] || die "$label is not a directory: $candidate"
    canonical="$(cd "$candidate" && pwd -P)"
  else
    parent="$(dirname -- "$candidate")"
    [[ -d "$parent" && ! -L "$parent" ]] || die "$label parent must be an existing non-symlink directory"
    canonical="$(cd "$parent" && pwd -P)/$(basename -- "$candidate")"
    mkdir -- "$candidate"
    [[ -d "$candidate" && ! -L "$candidate" ]] || die "could not create safe $label"
  fi
  [[ "$canonical" != "/" && "$canonical" != "$HOME" && "$canonical" != "$project_root" ]] || die "$label resolves to a forbidden directory"
  [[ "$(stat -c '%u' -- "$canonical")" == "$(id -u)" ]] || die "$label is not owned by the current user"
  printf '%s\n' "$canonical"
}

if (( ! work_dir_given )); then
  work_dir="$(mktemp -d "${TMPDIR:-/tmp}/pipewire-appimage-work.XXXXXX")"
  work_dir_created_by_script=1
  printf '%s\n' "$$ $script_start_epoch" > "$work_dir/.pipewire-appimage-work-owned"
else
  work_dir="$(validate_base_dir work-dir "$work_dir")"
fi
if (( ! output_dir_given )); then
  output_dir="$(mktemp -d "${TMPDIR:-/tmp}/pipewire-appimage-output.XXXXXX")"
  output_dir_created_by_script=1
else
  output_dir="$(validate_base_dir output-dir "$output_dir")"
fi

cleanup() {
  local canonical_work canonical_execution
  if [[ -n "$execution_dir" && -d "$execution_dir" && ! -L "$execution_dir" ]]; then
    canonical_work="$(cd "$work_dir" && pwd -P)"
    canonical_execution="$(cd "$execution_dir" && pwd -P)"
    if [[ "$canonical_execution" == "$canonical_work"/* && -f "$execution_dir/.pipewire-appimage-owned" ]]; then
      rm -rf -- "$execution_dir"
    fi
  fi
  if ((work_dir_created_by_script)) && [[ -d "$work_dir" && ! -L "$work_dir" && -f "$work_dir/.pipewire-appimage-work-owned" ]]; then
    canonical_work="$(cd "$work_dir" && pwd -P)"
    [[ "$canonical_work" != "/" && "$canonical_work" != "$HOME" && "$canonical_work" != "$project_root" ]] || return 0
    rm -rf -- "$work_dir"
  fi
}
trap cleanup EXIT

execution_dir="$(mktemp -d "$work_dir/run.XXXXXX")"
printf '%s\n' "$$ $script_start_epoch" > "$execution_dir/.pipewire-appimage-owned"
venv_dir="$execution_dir/venv"
appdir="$execution_dir/AppDir"
artifact="$output_dir/PipeWire-App-Launcher-x86_64.AppImage"

[[ ! -e "$appimagetool" && ! -L "$appimagetool" ]] && die "appimagetool does not exist: $appimagetool"
[[ -f "$appimagetool" && ! -L "$appimagetool" ]] || die "appimagetool must be a regular file and not a symlink"
[[ -x "$appimagetool" ]] || die "appimagetool is not executable: $appimagetool"
observed_tool_sha256="$(sha256sum -- "$appimagetool" | awk '{print $1}')"
[[ "$observed_tool_sha256" == "${appimagetool_sha256,,}" ]] || die "appimagetool SHA-256 mismatch (expected $appimagetool_sha256, observed $observed_tool_sha256)"
echo "appimagetool: $appimagetool (SHA-256 verified: $observed_tool_sha256)"
[[ -e "$runtime_file" && ! -L "$runtime_file" ]] || die "runtime file does not exist or is a symlink: $runtime_file"
[[ -f "$runtime_file" ]] || die "runtime file must be a regular file"
observed_runtime_sha256="$(sha256sum -- "$runtime_file" | awk '{print $1}')"
[[ "$observed_runtime_sha256" == "${runtime_sha256,,}" ]] || die "runtime SHA-256 mismatch (expected $runtime_sha256, observed $observed_runtime_sha256)"
echo "type-2 runtime: $runtime_file (SHA-256 verified: $observed_runtime_sha256)"
tool_version="$("$appimagetool" --version 2>&1 || true)"
echo "appimagetool version: ${tool_version:-unknown}"

[[ ! -e "$artifact" && ! -L "$artifact" ]] || die "refusing to overwrite existing artifact: $artifact"

xcb_cursor_lib="$(ldconfig -p 2>/dev/null | awk '/libxcb-cursor\.so\.0 / { print $NF; exit }')"
if [[ -z "$xcb_cursor_lib" || ! -f "$xcb_cursor_lib" ]]; then
  cat >&2 <<'EOF'
ERROR: libxcb-cursor.so.0 was not found on the build host.

Install the Ubuntu build dependency and run this script again:
  sudo apt update
  sudo apt install libxcb-cursor0
EOF
  exit 1
fi

cd "$execution_dir"
python3 -m venv "$venv_dir"
. "$venv_dir/bin/activate"
python -m pip install --upgrade pip
python -m pip install -r "$project_root/requirements.txt"
python -m PyInstaller --noconfirm --clean --windowed --paths "$project_root" --name pipewire-app-launcher --icon "$project_root/assets/pipewire-app-launcher.svg" "$project_root/pipewire_launcher/__main__.py"

mkdir -p "$appdir/usr/bin" "$appdir/usr/lib" "$appdir/usr/share/applications" "$appdir/usr/share/icons/hicolor/scalable/apps"
cp -a "$execution_dir/dist/pipewire-app-launcher/." "$appdir/usr/bin/"
cp -L "$xcb_cursor_lib" "$appdir/usr/lib/libxcb-cursor.so.0"
cp "$project_root/assets/pipewire-app-launcher.desktop" "$appdir/usr/share/applications/"
cp "$project_root/assets/pipewire-app-launcher.svg" "$appdir/usr/share/icons/hicolor/scalable/apps/"
ln -s usr/share/applications/pipewire-app-launcher.desktop "$appdir/pipewire-app-launcher.desktop"
ln -s usr/share/icons/hicolor/scalable/apps/pipewire-app-launcher.svg "$appdir/pipewire-app-launcher.svg"

printf '%s\n' '#!/bin/sh' 'export LD_LIBRARY_PATH="$APPDIR/usr/lib${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"' 'exec "$APPDIR/usr/bin/pipewire-app-launcher" "$@"' > "$appdir/AppRun"
chmod +x "$appdir/AppRun"

if ! LD_LIBRARY_PATH="$appdir/usr/lib${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}" ldd "$appdir/usr/bin/_internal/PySide6/Qt/plugins/platforms/libqxcb.so" | awk '/not found/ { missing=1; print > "/dev/stderr" } END { exit missing }'; then
  die "unresolved Qt xcb runtime libraries remain in AppDir"
fi

ARCH=x86_64 "$appimagetool" --runtime-file "$runtime_file" "$appdir" "$artifact"
[[ -f "$artifact" && ! -L "$artifact" && -s "$artifact" ]] || die "appimagetool did not produce a regular non-empty AppImage"
artifact="$(cd "$(dirname "$artifact")" && pwd -P)/$(basename "$artifact")"
echo "Created $artifact"
echo "AppImage size: $(stat -c '%s' -- "$artifact") bytes"
echo "AppImage SHA-256: $(sha256sum -- "$artifact" | awk '{print $1}')"
if ((work_dir_created_by_script)); then echo "Temporary work directory was cleaned after this run."; fi
if ((output_dir_created_by_script)); then echo "Temporary output directory preserved: $(dirname "$artifact")"; fi
