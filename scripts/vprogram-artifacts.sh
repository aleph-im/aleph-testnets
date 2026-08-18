#!/usr/bin/env bash
# Fetch the prebuilt V-PROGRAM test fixtures for tests/test_vprograms.py.
#
# The runtime (bundle + manifest) is the 2026.08.18.2 build (Linux 6.18,
# aleph-vm rev 39ec840a: v4-scoped udhcpc deconfig flush so the guest's
# IPv6 link-local survives, aleph-vm#1126, on top of #1125's DAD wait;
# measurement 952167b9..., platform roothash unchanged), published on Aleph mainnet native storage; native
# storage is content-addressed by sha256, so the fetch URL doubles as the
# pin. The fib workload still comes from the vprogram-fixtures-1 GitHub
# prerelease (unchanged).
#
#   1. snp-image.tar.gz       — runtime bundle (OVMF, kernel, initrd,
#                               dm-verity platform rootfs + hash tree)
#   2. manifest-template.json — aleph-vprogram-runtime v1 manifest; the test
#                               patches bundle.ref to the per-run STORE hash
#                               after uploading the bundle to the test net
#   3. fib-workload.ext4      — fib-service workload volume (GET /health and
#                               /fib/{n} on :8080)
#
# Everything is verified against pinned sha256s: the artifacts are immutable
# fixtures, so a mismatch means a broken download or a tampered source, and
# either must fail the run.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
OUT_DIR="$REPO_ROOT/.local/vprogram"

RELEASE_URL="https://github.com/aleph-im/aleph-testnets/releases/download/vprogram-fixtures-1"
ALEPH_STORAGE_URL="https://official.aleph.cloud/api/v0/storage/raw"

# sha256 of every fixture. Aleph-storage assets are fetched by this hash;
# release assets by name.
declare -A CHECKSUMS=(
    [snp-image.tar.gz]="b2795ab58f23bf25674de56c6b821f0b78e0432ef716b3802830a43965d11bbe"
    [manifest-template.json]="eb0dd8e9b118a36cd6f80d8c9903ae66fb02142c6b140987508aae5cc39e8a50"
    [fib-workload.ext4]="5a04d7949c488acbd909d2a63190fc9810d0d61263274c294b714625f8193db0"
)
declare -A SOURCES=(
    [snp-image.tar.gz]="$ALEPH_STORAGE_URL/b2795ab58f23bf25674de56c6b821f0b78e0432ef716b3802830a43965d11bbe"
    [manifest-template.json]="$ALEPH_STORAGE_URL/eb0dd8e9b118a36cd6f80d8c9903ae66fb02142c6b140987508aae5cc39e8a50"
    [fib-workload.ext4]="$RELEASE_URL/fib-workload.ext4"
)

mkdir -p "$OUT_DIR"
# Drop stale fixtures from the previous runtime generation.
rm -f "$OUT_DIR/bundle-info.json"

for asset in "${!CHECKSUMS[@]}"; do
    dest="$OUT_DIR/$asset"
    want="${CHECKSUMS[$asset]}"
    if [ -f "$dest" ] && echo "$want  $dest" | sha256sum -c --quiet 2>/dev/null; then
        echo "    Cached: $asset"
        continue
    fi
    echo "==> Downloading $asset..."
    curl -fsSL -o "$dest" "${SOURCES[$asset]}"
    echo "$want  $dest" | sha256sum -c --quiet
done

echo "==> V-PROGRAM fixtures ready in $OUT_DIR:"
ls -la "$OUT_DIR"
