#!/usr/bin/env bash
# Fetch the prebuilt V-PROGRAM test fixtures for tests/test_vprograms.py.
#
# The runtime (bundle + manifest) is the 2026.08.31 "1.1" build (aleph-vm
# rev c5391963 = #1176: V-PROGRAM verified data volumes; exec measurement
# 37127d19.../platform roothash d8b9a199..., compose b66b44bb.../cb03a9fe...).
# It adds the {verified_volumes} cmdline slot and the guest /volumes/<i>
# verity mounts that tests/test_vprogram_compose.py's volume test relies
# on. Published on Aleph mainnet native storage; native storage is
# content-addressed by sha256, so the fetch URL doubles as the pin. The
# manifest template carries the MAINNET bundle ref (it is the published
# manifest verbatim); conftest patches bundle.ref to the per-run testnet
# STORE hash before uploading, so the stale ref is inert.
# The fib workload is the 2026-08-18 nix rebuild (same fib-service
# behavior, nixpkgs 26.05 toolchain), fetched from mainnet storage like
# the rest.
#
#   1. snp-image.tar.gz       — runtime bundle (OVMF, kernel, initrd,
#                               dm-verity platform rootfs + hash tree)
#   2. manifest-template.json — aleph-vprogram-runtime v1 manifest; the test
#                               patches bundle.ref to the per-run STORE hash
#                               after uploading the bundle to the test net
#   3. fib-workload.ext4      — fib-service workload volume (GET /health and
#                               /fib/{n} on :8080)
#   4. compose-image.tar.gz   — aleph.compose/1 runtime bundle (same 2026.08.31
#                               build; podman + podman-compose platform rootfs,
#                               compose measurement b66b44bb...). 268 MB, above
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
    [snp-image.tar.gz]="1bdfb5f6bdc0cdb56ac1a50352b20261ea416a6c76c898b96371817d98707694"
    [manifest-template.json]="c58014bcaf4c715230bc563baa778739b4df7566f174044a8e41f0989e797c6c"
    [fib-workload.ext4]="9b9c4ffe03b35ecec6ae418180e298f1f89fd74b71b9c77371271e43d0d619b0"
    [compose-image.tar.gz]="4132c4c2c01e386ba26f588ff22c9e5c0a19273d2376459dd66f717a31d71a81"
    [compose-manifest-template.json]="5bdb3914f03d9e06c0bcf4e3703d7e7700ff1eba9641b14a0fe02c15f434c1db"
)
declare -A SOURCES=(
    [snp-image.tar.gz]="$ALEPH_STORAGE_URL/1bdfb5f6bdc0cdb56ac1a50352b20261ea416a6c76c898b96371817d98707694"
    [manifest-template.json]="$ALEPH_STORAGE_URL/c58014bcaf4c715230bc563baa778739b4df7566f174044a8e41f0989e797c6c"
    [fib-workload.ext4]="$ALEPH_STORAGE_URL/9b9c4ffe03b35ecec6ae418180e298f1f89fd74b71b9c77371271e43d0d619b0"
    [compose-image.tar.gz]="$ALEPH_IPFS_URL/QmZGppZrYyezBNU3V91GfvpqAGQJ3dAByqHERiH6KKW1S3"
    [compose-manifest-template.json]="$ALEPH_STORAGE_URL/5bdb3914f03d9e06c0bcf4e3703d7e7700ff1eba9641b14a0fe02c15f434c1db"
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
