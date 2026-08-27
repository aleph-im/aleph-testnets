#!/usr/bin/env bash
# Fetch the prebuilt V-PROGRAM test fixtures for tests/test_vprograms.py.
#
# The runtime (bundle + manifest) is the 2026.08.20 build (Linux 6.18,
# aleph-vm rev ba690c65 = #1131 tip: attest-agent secrets work + init
# refactor on top of the 08-18 udhcpc fixes; measurement 04729caf...,
# platform roothash unchanged), published on Aleph mainnet native storage;
# native storage is content-addressed by sha256, so the fetch URL doubles
# as the pin. The manifest template carries the MAINNET bundle ref (it is
# the published manifest verbatim); conftest patches bundle.ref to the
# per-run testnet STORE hash before uploading, so the stale ref is inert.
# The fib workload is now the 2026-08-18 nix rebuild (same fib-service
# behavior, nixpkgs 26.05 toolchain), fetched from mainnet storage like
# the rest instead of the vprogram-fixtures-1 GitHub prerelease.
#
#   1. snp-image.tar.gz       — runtime bundle (OVMF, kernel, initrd,
#                               dm-verity platform rootfs + hash tree)
#   2. manifest-template.json — aleph-vprogram-runtime v1 manifest; the test
#                               patches bundle.ref to the per-run STORE hash
#                               after uploading the bundle to the test net
#   3. fib-workload.ext4      — fib-service workload volume (GET /health and
#                               /fib/{n} on :8080)
#   4. compose-image.tar.gz   — aleph.compose/1 runtime bundle (same 2026.08.20
#                               build; podman + podman-compose platform rootfs,
#                               compose measurement bc9fd3c9...). 297 MB, above
#                               the mainnet native-storage limit, so it lives on
#                               mainnet IPFS and is fetched by CID; the pinned
#                               sha256 check below still guards its content.
#   5. compose-manifest-template.json — the compose runtime's manifest
#                               (workload contract aleph.compose/1), bundle.ref
#                               patched per-run like the vprogram one
#
# Everything is verified against pinned sha256s: the artifacts are immutable
# fixtures, so a mismatch means a broken download or a tampered source, and
# either must fail the run.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
OUT_DIR="$REPO_ROOT/.local/vprogram"

ALEPH_STORAGE_URL="https://official.aleph.cloud/api/v0/storage/raw"
ALEPH_IPFS_URL="https://ipfs.aleph.cloud/ipfs"

# sha256 of every fixture; native-storage assets are fetched from Aleph
# storage by this same hash, the IPFS-hosted compose bundle by its CID.
declare -A CHECKSUMS=(
    [snp-image.tar.gz]="6a19d1709333cd4ac5e31708432e6c9d2c9241f9e726dc44865b2a7fe9691fad"
    [manifest-template.json]="852c7b13745936fb80dc662ca169dfa417132fcff4f80c295cbe8573f8c5825c"
    [fib-workload.ext4]="9b9c4ffe03b35ecec6ae418180e298f1f89fd74b71b9c77371271e43d0d619b0"
    [compose-image.tar.gz]="a30b27ccfcd9b45cb3946ea71d0762d90275cbf1602ed50021d70699d5aef270"
    [compose-manifest-template.json]="3f7e5a2580185094690d22d2cc417e8d6243e8cfb6935453f8bcd47f3883c7bc"
)
declare -A SOURCES=(
    [snp-image.tar.gz]="$ALEPH_STORAGE_URL/6a19d1709333cd4ac5e31708432e6c9d2c9241f9e726dc44865b2a7fe9691fad"
    [manifest-template.json]="$ALEPH_STORAGE_URL/852c7b13745936fb80dc662ca169dfa417132fcff4f80c295cbe8573f8c5825c"
    [fib-workload.ext4]="$ALEPH_STORAGE_URL/9b9c4ffe03b35ecec6ae418180e298f1f89fd74b71b9c77371271e43d0d619b0"
    [compose-image.tar.gz]="$ALEPH_IPFS_URL/QmUNKALvBTf4sFaaUxwWJnLMY6V6qu2tXdqY7tHNcEA6nk"
    [compose-manifest-template.json]="$ALEPH_STORAGE_URL/3f7e5a2580185094690d22d2cc417e8d6243e8cfb6935453f8bcd47f3883c7bc"
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
