#!/usr/bin/env bash
# Unpack the user-downloaded Insta360 MediaSDK .deb without root and wire it
# up for ai-autoedit. The SDK is licensed (EULA forbids redistribution) —
# download it yourself after approval at https://www.insta360.com/sdk/apply,
# then run:
#
#   ./scripts/setup_insta360_sdk.sh /path/to/MediaSDK-*-linux-amd64.deb
#
# Result: insta360-sdk/opt-extract/opt/MediaSDK-*/bin/MediaSDKTest — the
# path config.ini [paths] insta360_mediasdk points at by default.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
DEB="${1:-}"

if [[ -z "$DEB" ]]; then
    # Try to find the deb inside an already-unzipped SDK download
    DEB="$(find "$REPO_ROOT/insta360-sdk" -maxdepth 3 -name 'MediaSDK-*-linux-amd64.deb' 2>/dev/null | head -1 || true)"
fi
[[ -f "$DEB" ]] || { echo "usage: $0 <MediaSDK-*-linux-amd64.deb>"; exit 1; }

OUT="$REPO_ROOT/insta360-sdk/opt-extract"
echo "Extracting $(basename "$DEB") → $OUT (no root needed)"
mkdir -p "$OUT"
dpkg -x "$DEB" "$OUT"

SDK_DIR="$(find "$OUT/opt" -maxdepth 1 -type d -name 'MediaSDK-*' | head -1)"
[[ -n "$SDK_DIR" ]] || { echo "extraction failed — no opt/MediaSDK-* dir"; exit 1; }

# The binary links Ubuntu 22.04's libtiff.so.5; modern hosts ship .so.6.
# A symlink inside the SDK's own lib dir (covered by its RPATH) fixes it
# without touching the system.
SYS_TIFF="$(ldconfig -p | awk '/libtiff\.so\.6/{print $NF; exit}')"
if [[ -n "$SYS_TIFF" && ! -e "$SDK_DIR/lib/libtiff.so.5" ]]; then
    ln -sf "$SYS_TIFF" "$SDK_DIR/lib/libtiff.so.5"
    echo "Linked libtiff.so.5 → $SYS_TIFF"
fi

BIN="$SDK_DIR/bin/MediaSDKTest"
if "$BIN" 2>&1 | head -3 | grep -q "SDK version"; then
    echo "OK: $BIN runs."
else
    echo "WARNING: $BIN did not start — check: ldd $BIN | grep 'not found'"
fi

REL="${BIN#"$REPO_ROOT"/}"
echo
echo "Set in config.ini:"
echo "  [paths]"
echo "  insta360_mediasdk = $REL"
echo "then restart the container (the SDK dir is bind-mounted read-only)."
