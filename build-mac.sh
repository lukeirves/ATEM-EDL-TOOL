#!/bin/sh
set -eu

cd "$(dirname "$0")"
export PYINSTALLER_CONFIG_DIR="$PWD/.pyinstaller"
rm -f 'dist/ATEM-EDL-TOOL-Apple-Silicon.zip' 'dist/ATEM-EDL-TOOL-Intel.zip'

PYTHON_BIN="${PYTHON_BIN:-python3.12}"
"$PYTHON_BIN" -m venv .build-venv
.build-venv/bin/python -m pip install -r requirements-build.txt
.build-venv/bin/python -m PyInstaller --noconfirm --clean atemlogger.spec

codesign --verify --deep --strict 'dist/ATEM-EDL-TOOL.app'
echo "App created at dist/ATEM-EDL-TOOL.app"

if [ -n "${ATEMLOGGER_CODESIGN_IDENTITY:-}" ]; then
    if ! codesign -dv --verbose=2 'dist/ATEM-EDL-TOOL.app' 2>&1 | grep -Fq 'Authority=Developer ID Application:'; then
        echo 'The app was not signed with a Developer ID Application certificate.' >&2
        exit 1
    fi
    case "$(uname -m)" in
        arm64) ARCHIVE='dist/ATEM-EDL-TOOL-Apple-Silicon.zip' ;;
        x86_64) ARCHIVE='dist/ATEM-EDL-TOOL-Intel.zip' ;;
        *) echo 'Unsupported Mac architecture for release packaging' >&2; exit 1 ;;
    esac
    ditto -c -k --sequesterRsrc --keepParent 'dist/ATEM-EDL-TOOL.app' "$ARCHIVE"
    echo "Signed app ZIP created at $ARCHIVE"
else
    echo 'Ad hoc signed local build only. Set ATEMLOGGER_CODESIGN_IDENTITY for a shareable ZIP.'
fi
