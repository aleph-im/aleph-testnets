#!/usr/bin/env bash
# Fetch the prebuilt V-PROGRAM test fixtures for tests/test_vprograms.py.
#
# The runtime (bundle + manifest) is the 2026.09.01 build: the 1.1
# verified-volumes runtime (aleph-vm c5391963, #1176: {verified_volumes}
# cmdline slot + guest /volumes/<i> verity mounts) plus the SNP guest
# kernel fix (aleph-vm#1184: CONFIG_X86_PAT/CONFIG_MTRR; the 2026.08.31
# bundles hung before console on real SEV-SNP and were never usable).
# Hosted as assets on this repo's vprogram-fixtures-2 prerelease until
# the fixed runtimes are republished on mainnet; sha256 pins below are
# still the integrity gate. The manifest templates carry the bundle
# sha256 as a placeholder ref; conftest patches bundle.ref to the
# per-run testnet STORE hash before uploading, so the stale ref is inert.
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
#   4. compose-image.tar.gz   — aleph.compose/1 runtime bundle (same 2026.09.01
#                               build; podman + podman-compose platform rootfs).
#                               268 MB, fetched from the same prerelease.
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
FIXTURES_URL="https://github.com/aleph-im/aleph-testnets/releases/download/vprogram-fixtures-2"

# sha256 of every fixture; native-storage assets are fetched from Aleph
# storage by this same hash, the IPFS-hosted compose bundle by its CID.
declare -A CHECKSUMS=(
    [snp-image.tar.gz]="a8c9a8015a68986dd185f0f47eb08e9492d19273fe3f0166388619129693f63b"
    [manifest-template.json]="3755ada182b57dbb58b1abf263cb7c4906e5181236d3694bb393f5de23f399e5"
    [fib-workload.ext4]="9b9c4ffe03b35ecec6ae418180e298f1f89fd74b71b9c77371271e43d0d619b0"
    [compose-image.tar.gz]="7716df19f15e9793cd755d14665823d6cb8fdb083ac5878a3c4cf95ab813af60"
    [compose-manifest-template.json]="55d568bc00cfe001d505c780a3b648eb6ec5a81752035eda9f73116d4cdbc37b"
)
declare -A SOURCES=(
    [snp-image.tar.gz]="$FIXTURES_URL/exec-snp-image.tar.gz"
    [manifest-template.json]="$FIXTURES_URL/exec-manifest-template.json"
    [fib-workload.ext4]="$ALEPH_STORAGE_URL/9b9c4ffe03b35ecec6ae418180e298f1f89fd74b71b9c77371271e43d0d619b0"
    [compose-image.tar.gz]="$FIXTURES_URL/compose-snp-image.tar.gz"
    [compose-manifest-template.json]="$FIXTURES_URL/compose-manifest-template.json"
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
