#!/usr/bin/env bash
# Fetch the prebuilt V-PROGRAM test fixtures for tests/test_vprograms.py.
#
# The runtimes (bundle + manifest) are the 2026.09.22 build from aleph-vm
# 80e48a8e (dev-2.1.2: main at 0295bb08 plus the 2.2 work, aleph-tee
# without openssl, the attest-agent proxy fixes and the GPU stack).
# Hosted as assets on this repo's vprogram-fixtures-3 prerelease until
# the runtimes are republished on mainnet; sha256 pins below are
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
#   4. compose-image.tar.gz   — aleph.compose/1 runtime bundle (same 2026.09.22
#                               build; podman + podman-compose platform rootfs).
#                               268 MB, fetched from the same prerelease.
#   5. compose-manifest-template.json — the compose runtime's manifest
#                               (workload contract aleph.compose/1), bundle.ref
#                               patched per-run like the vprogram one
#
# With --gpu (or VPROGRAM_GPU=1), also fetches the GPU fixtures:
#   6. gpu-snp-image.tar.gz: runtime bundle for the CUDA V-PROGRAM
#   7. gpu-manifest-template.json: its manifest, bundle.ref patched per-run
#   8. cuda-workload.ext4: cuda-probe workload volume
#
# Everything is verified against pinned sha256s: the artifacts are immutable
# fixtures, so a mismatch means a broken download or a tampered source, and
# either must fail the run.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
OUT_DIR="$REPO_ROOT/.local/vprogram"

ALEPH_STORAGE_URL="https://official.aleph.cloud/api/v0/storage/raw"
ALEPH_IPFS_URL="https://ipfs.aleph.cloud/ipfs"
FIXTURES_URL="https://github.com/aleph-im/aleph-testnets/releases/download/vprogram-fixtures-3"
GPU_FIXTURES_URL="https://github.com/aleph-im/aleph-testnets/releases/download/vprogram-fixtures-gpu-3"

GPU=0
for arg in "$@"; do
    case "$arg" in
        --gpu) GPU=1 ;;
    esac
done
if [ "${VPROGRAM_GPU:-0}" = "1" ]; then GPU=1; fi

# sha256 of every fixture; native-storage assets are fetched from Aleph
# storage by this same hash, the IPFS-hosted compose bundle by its CID.
declare -A CHECKSUMS=(
    [snp-image.tar.gz]="0d7c675cd36f050c3f5ff9e00bb2bc051700009a781599ad3b8b6ef856444a08"
    [manifest-template.json]="dd542b6fb8dbb65f29091999f802aa3931b8cca6a0bcce76fc0145716f30680a"
    [fib-workload.ext4]="9b9c4ffe03b35ecec6ae418180e298f1f89fd74b71b9c77371271e43d0d619b0"
    [compose-image.tar.gz]="b603d6b33e12186c45aae1e16d2f99c33581d05fd517a607b2890a5ee9c90ff8"
    [compose-manifest-template.json]="02d8ff005eb8febe33c7a7037336ef37e6194d1d930f9e707e6c0d85d5cb8695"
)
declare -A SOURCES=(
    [snp-image.tar.gz]="$FIXTURES_URL/exec-snp-image.tar.gz"
    [manifest-template.json]="$FIXTURES_URL/exec-manifest-template.json"
    [fib-workload.ext4]="$ALEPH_STORAGE_URL/9b9c4ffe03b35ecec6ae418180e298f1f89fd74b71b9c77371271e43d0d619b0"
    [compose-image.tar.gz]="$FIXTURES_URL/compose-snp-image.tar.gz"
    [compose-manifest-template.json]="$FIXTURES_URL/compose-manifest-template.json"
)

if [ "$GPU" = "1" ]; then
    CHECKSUMS[gpu-snp-image.tar.gz]="38311062ee2f3198e52154ffec98340c4d6c479650a1f4da1fd61f2386bfbcbb"
    CHECKSUMS[gpu-manifest-template.json]="0155e6908be033745389e73012067bc46c5f8b9ba7af1fc5a7803b18a742678f"
    CHECKSUMS[cuda-workload.ext4]="32a12fa02855ca2def170a46788910136ba0fb89c72a611389b1f4b430fea3e2"
    SOURCES[gpu-snp-image.tar.gz]="$GPU_FIXTURES_URL/gpu-snp-image.tar.gz"
    SOURCES[gpu-manifest-template.json]="$GPU_FIXTURES_URL/gpu-manifest-template.json"
    SOURCES[cuda-workload.ext4]="$GPU_FIXTURES_URL/cuda-workload.ext4"
fi

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
