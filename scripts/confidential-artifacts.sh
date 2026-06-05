#!/usr/bin/env bash
# Prepare client-side artifacts for the confidential VM test. Run from the
# machine that runs pytest (the CCN droplet in CI).
#
#   1. sevctl        — copied from the TEE server (the aleph-vm .deb installs
#                      it at /opt/sevctl) into ./bin, which run_tests puts on
#                      PATH. Keeps client and CRN sevctl in lockstep.
#   2. OVMF firmware — canonical confidential firmware blob, downloaded from
#                      aleph.cloud by its well-known item hash and verified
#                      against its known sha256. The test uploads it to the
#                      test CCN and passes it to `confidential start`.
#   3. Encrypted rootfs — built on the TEE server with the vendored
#                      scripts/confidential-image/ scripts (guestmount +
#                      cryptsetup need root, and the static server hosts the
#                      cache), cached under /opt/aleph-ci-cache keyed by the
#                      build inputs, then copied locally.
#
# Environment variables:
#   TEE_HOST       Address of the TEE server (required)
#   TEE_USER       SSH user on the TEE server (default: root; non-root needs
#                  passwordless sudo)
#   SEVCTL_HOST    Host to copy /opt/sevctl from (default: TEE_HOST). Prefer a
#                  debian-12 CRN: its sevctl is built against the oldest glibc
#                  and therefore runs on this (newer) machine, whereas the TEE
#                  server's distro — and its .deb's glibc baseline — may be
#                  newer than ours.
#   SEVCTL_USER    SSH user on SEVCTL_HOST (default: TEE_USER if SEVCTL_HOST
#                  is the TEE server, else root)
#   SSH_KEY_FILE   SSH private key (default: ~/.ssh/id_ed25519)
#   ALEPH_TESTNET_CONFIDENTIAL_PASSWORD
#                  Disk password baked into the image (default: test-password —
#                  a fixed, non-secret test value; must match what the test
#                  passes to `confidential start`, see tests/conftest.py)
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
OUT_DIR="$REPO_ROOT/.local/confidential"
BIN_DIR="$REPO_ROOT/bin"
IMAGE_DIR="$REPO_ROOT/scripts/confidential-image"

TEE_HOST="${TEE_HOST:?TEE_HOST must be set}"
TEE_USER="${TEE_USER:-root}"
SEVCTL_HOST="${SEVCTL_HOST:-$TEE_HOST}"
if [ "$SEVCTL_HOST" = "$TEE_HOST" ]; then
    SEVCTL_USER="${SEVCTL_USER:-$TEE_USER}"
else
    SEVCTL_USER="${SEVCTL_USER:-root}"
fi
SSH_KEY_FILE="${SSH_KEY_FILE:-$HOME/.ssh/id_ed25519}"
DISK_PASSWORD="${ALEPH_TESTNET_CONFIDENTIAL_PASSWORD:-test-password}"

# Canonical confidential OVMF blob on aleph.cloud, addressed by its storage
# file hash — which for the storage engine is the sha256 of the blob itself,
# so the same constant doubles as the download-integrity check. (The STORE
# message wrapping the file is ba5bb13f3abca960b101a759be162b229e2b7e93ecad9
# d1307e54de887f177ff and returns 404 on the raw endpoint — don't use it.)
# The measurement anchor the test actually trusts is the local blob passed
# via `--firmware-file`.
FIRMWARE_SHA256="89b76b0e64fe9015084fbffdf8ac98185bafc688bfe7a0b398585c392d03c7ee"
FIRMWARE_URL="https://api2.aleph.im/api/v0/storage/raw/${FIRMWARE_SHA256}"

BASE_IMAGE_URL="https://cloud.debian.org/images/cloud/bookworm/latest/debian-12-genericcloud-amd64.qcow2"
# Must hold the extracted Debian rootfs (~1.2 GB) + boot partition + LUKS
# headroom, and stay under the CCN's 4 GiB upload limit.
IMAGE_SIZE="3GB"
CACHE_DIR="/opt/aleph-ci-cache"

SSH_OPTS=(-o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -i "$SSH_KEY_FILE")

tee_ssh() {
    local cmd="$1"
    if [ "$TEE_USER" = "root" ]; then
        ssh "${SSH_OPTS[@]}" "root@$TEE_HOST" "$cmd"
    else
        ssh "${SSH_OPTS[@]}" "$TEE_USER@$TEE_HOST" "sudo -n bash -c $(printf '%q' "$cmd")"
    fi
}

mkdir -p "$OUT_DIR" "$BIN_DIR"

# --- 1. sevctl ---------------------------------------------------------------
# Stage through /tmp with sudo: /opt/sevctl is normally world-readable, but a
# non-root user must not depend on that.
sevctl_ssh() {
    local cmd="$1"
    if [ "$SEVCTL_USER" = "root" ]; then
        ssh "${SSH_OPTS[@]}" "root@$SEVCTL_HOST" "$cmd"
    else
        ssh "${SSH_OPTS[@]}" "$SEVCTL_USER@$SEVCTL_HOST" "sudo -n bash -c $(printf '%q' "$cmd")"
    fi
}

echo "==> Fetching sevctl from $SEVCTL_HOST..."
sevctl_ssh "cp /opt/sevctl /tmp/sevctl && chmod 644 /tmp/sevctl"
scp "${SSH_OPTS[@]}" "$SEVCTL_USER@$SEVCTL_HOST:/tmp/sevctl" "$BIN_DIR/sevctl"
sevctl_ssh "rm -f /tmp/sevctl"
chmod +x "$BIN_DIR/sevctl"
# Fail here, not at init-session time, if the binary doesn't run on this host
# (e.g. built against a newer glibc than ours). -V matches aleph-vm's own
# packaging smoke test.
"$BIN_DIR/sevctl" -V >/dev/null

# --- 2. OVMF firmware --------------------------------------------------------
firmware="$OUT_DIR/OVMF.fd"
if [ ! -f "$firmware" ] || ! echo "$FIRMWARE_SHA256  $firmware" | sha256sum -c --quiet 2>/dev/null; then
    echo "==> Downloading confidential OVMF firmware..."
    curl -fsSL -o "$firmware" "$FIRMWARE_URL"
    echo "$FIRMWARE_SHA256  $firmware" | sha256sum -c --quiet
fi
echo "    Firmware OK: $firmware"

# --- 3. Encrypted rootfs -------------------------------------------------------
# Cache key over the build inputs: editing the vendored scripts, the base
# image URL/size or the password triggers a rebuild on the TEE server.
cache_key=$( (cat "$IMAGE_DIR/build_debian_image.sh" "$IMAGE_DIR/setup_debian_rootfs.sh"
              echo "$BASE_IMAGE_URL"; echo "$IMAGE_SIZE"; echo "$DISK_PASSWORD") | sha256sum | cut -c1-16)
cached_img="$CACHE_DIR/rootfs-$cache_key.img"

if ! tee_ssh "test -f '$cached_img'"; then
    echo "==> Cache miss — building encrypted rootfs on the TEE server (~5-10 min)..."
    for tool in guestmount parted cryptsetup sudo; do
        if ! tee_ssh "command -v $tool >/dev/null"; then
            echo "ERROR: '$tool' not found on the TEE server. See docs/tee-server.md." >&2
            exit 1
        fi
    done

    workdir=$(tee_ssh "mkdir -p '$CACHE_DIR' && mktemp -d '$CACHE_DIR/build-XXXXXX'")

    echo "    Uploading build scripts..."
    scp "${SSH_OPTS[@]}" "$IMAGE_DIR/build_debian_image.sh" "$IMAGE_DIR/setup_debian_rootfs.sh" \
        "$TEE_USER@$TEE_HOST:/tmp/"
    tee_ssh "mv /tmp/build_debian_image.sh /tmp/setup_debian_rootfs.sh '$workdir/'"

    echo "    Downloading base image..."
    tee_ssh "curl -fsSL -o '$workdir/base.qcow2' '$BASE_IMAGE_URL'"

    echo "    Extracting rootfs (guestmount)..."
    tee_ssh "mkdir -p '$workdir/mnt' '$workdir/rootfs' && \
             guestmount --format=qcow2 -a '$workdir/base.qcow2' -o allow_other -i '$workdir/mnt' && \
             cp --archive '$workdir'/mnt/* '$workdir/rootfs/' && \
             guestunmount '$workdir/mnt'"

    echo "    Building encrypted image..."
    tee_ssh "cd '$workdir' && bash build_debian_image.sh \
             --rootfs-dir ./rootfs -o '$cached_img.tmp' \
             --image-size '$IMAGE_SIZE' --password '$DISK_PASSWORD'"
    tee_ssh "mv '$cached_img.tmp' '$cached_img' && chmod 644 '$cached_img'"

    echo "    Pruning old cache entries (keeping the 2 newest)..."
    tee_ssh "ls -t '$CACHE_DIR'/rootfs-*.img 2>/dev/null | tail -n +3 | xargs -r rm -f"
    tee_ssh "rm -rf '$workdir'"
else
    echo "==> Encrypted rootfs cache hit: $cached_img"
fi

echo "==> Copying encrypted rootfs locally..."
scp "${SSH_OPTS[@]}" "$TEE_USER@$TEE_HOST:$cached_img" "$OUT_DIR/rootfs.img"

echo "==> Confidential artifacts ready:"
echo "    $BIN_DIR/sevctl"
echo "    $firmware"
echo "    $OUT_DIR/rootfs.img"
