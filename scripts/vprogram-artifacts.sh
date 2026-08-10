#!/usr/bin/env bash
# Fetch the prebuilt V-PROGRAM test fixtures for tests/test_vprograms.py.
#
# The fixtures are nix-reproducible builds of the aleph-vm measured runtime
# image and the fib-service demo workload (aleph-vm od/vprogram-integration),
# published once as a GitHub prerelease on this repo (vprogram-fixtures-1) so
# CI never has to run the nix build. Rebuild instructions live in the release
# notes (scripts/vprogram_bundle.py in aleph-vm).
#
#   1. snp-image.tar.gz       — runtime bundle (OVMF, kernel, initrd,
#                               dm-verity platform rootfs + hash tree)
#   2. bundle-info.json       — bundle metadata sidecar (sha256/size/members)
#   3. manifest-template.json — aleph-vprogram-runtime v1 manifest with
#                               bundle.ref zeroed; the test patches in the
#                               per-run STORE hash after uploading the bundle
#   4. fib-workload.ext4      — fib-service workload volume (GET /health and
#                               /fib/{n} on :8080)
#
# Everything is verified against pinned sha256s: the release assets are
# immutable fixtures, so a mismatch means a broken download or a tampered
# release, and either must fail the run.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
OUT_DIR="$REPO_ROOT/.local/vprogram"

RELEASE_URL="https://github.com/aleph-im/aleph-testnets/releases/download/vprogram-fixtures-1"

# sha256 of every asset in the vprogram-fixtures-1 release.
declare -A CHECKSUMS=(
    [snp-image.tar.gz]="ea72dc88f4ff1e5e7880617aa7cea7544e3aa6dcd88a64c9c78a5f41cb21238e"
    [bundle-info.json]="5969b490cf06f681fdf1aa86c3c6f28117ffc1b9652ae0922ddcb64edbf2066e"
    [manifest-template.json]="ef5071bb9278dc6b226d6714aca2de5743f4644549730da5fbbc23e0ca6247d1"
    [fib-workload.ext4]="5a04d7949c488acbd909d2a63190fc9810d0d61263274c294b714625f8193db0"
)

mkdir -p "$OUT_DIR"

for asset in "${!CHECKSUMS[@]}"; do
    dest="$OUT_DIR/$asset"
    want="${CHECKSUMS[$asset]}"
    if [ -f "$dest" ] && echo "$want  $dest" | sha256sum -c --quiet 2>/dev/null; then
        echo "    Cached: $asset"
        continue
    fi
    echo "==> Downloading $asset..."
    curl -fsSL -o "$dest" "$RELEASE_URL/$asset"
    echo "$want  $dest" | sha256sum -c --quiet
done

echo "==> V-PROGRAM fixtures ready in $OUT_DIR:"
ls -la "$OUT_DIR"
