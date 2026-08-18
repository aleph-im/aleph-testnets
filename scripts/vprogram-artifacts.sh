#!/usr/bin/env bash
# Fetch the prebuilt V-PROGRAM test fixtures for tests/test_vprograms.py.
#
# The runtime (bundle + manifest) is the 2026.08.18.1 build (Linux 6.18,
# aleph-vm rev d9e0c5c6: adds the link-local DAD wait before the guest's
# DHCPv6 solicit, aleph-vm#1125; measurement c632109e..., platform
# roothash unchanged), published on Aleph mainnet native storage; native
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
    [snp-image.tar.gz]="a07b48bde01f39506ec10e0176aa366eac202ff1d153b3b190b0ecf3ee119e46"
    [manifest-template.json]="9c585dc0b9d37415bbc736242a39cfa38bacdda6abbfe84cebc28373c054b454"
    [fib-workload.ext4]="5a04d7949c488acbd909d2a63190fc9810d0d61263274c294b714625f8193db0"
)
declare -A SOURCES=(
    [snp-image.tar.gz]="$ALEPH_STORAGE_URL/a07b48bde01f39506ec10e0176aa366eac202ff1d153b3b190b0ecf3ee119e46"
    [manifest-template.json]="$ALEPH_STORAGE_URL/9c585dc0b9d37415bbc736242a39cfa38bacdda6abbfe84cebc28373c054b454"
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
