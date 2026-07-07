#!/usr/bin/env bash
# Prepare the SEV-SNP validation artifacts. Run on the machine that runs pytest
# (the CCN droplet in CI). Sibling of confidential-artifacts.sh, but for the
# Phase 3 measured-boot SEV-SNP flow (tests/test_vm_snp.py) rather than the
# legacy AMD-SEV encrypted-rootfs flow.
#
# What it stages
# --------------
#   1. aleph-attest-cli  — the client-side TEE attestation verifier (Rust,
#      candidate branch crate aleph-attest-cli). Placed in ./bin (which
#      local-up.sh run_tests puts on PATH) AND scp'd to the SNP (TEE) host,
#      because the test runs it ON the SNP host: it must reach the guest at
#      https://<guest-tap-ip>:8443, and that private tap IP is only routable
#      from the host itself, not cross-host from the CCN.
#
#   2. Measured image    — the nix flake `image` output from the candidate
#      aleph-vm branch: a directory with bzImage, initrd, rootfs.ext4 (+
#      rootfs.ext4.verity hash tree and rootfs.ext4.roothash sidecars), OVMF.fd
#      and measurement.hex. scp'd to the SNP host under $SNP_IMAGE_HOST_DIR;
#      the test's CreateVm points at these paths.
#
#   3. Measurement        — measurement.hex (48-byte SHA-384, hex) copied to
#      $SNP_STAGE_DIR/measurement, which local-up.sh exports as
#      ALEPH_TESTNET_SNP_MEASUREMENT and the test pins with
#      `aleph-attest-cli --expected-measurement`.
#
# Build vs receive
# ----------------
# The heavy builds (nix measured image + cargo attest-cli) are expensive and
# need toolchains this (CCN) box does not have. The PRIMARY path is therefore
# RECEIVE: the GitHub Actions workflow builds them on its runner (which has nix
# + cargo) and rsyncs them into $SNP_STAGE_DIR before this script runs; here we
# only validate, record the measurement, and distribute to the SNP host.
#
# A local BUILD fallback exists for operators running by hand: set
# ALEPH_VM_SRC=/path/to/aleph-vm (a candidate-branch checkout) and this script
# will `nix build` the image and `cargo build` the CLI itself, if nix/cargo are
# present. See the ASSUMPTIONS block at the bottom.
#
# Environment variables
#   SNP_HOST / TEE_HOST   SNP (TEE) host address (required; TEE_HOST accepted so
#                         the workflow can pass the same AMD_SEV_SNP_HOST secret
#                         it passes to confidential-artifacts.sh)
#   SNP_USER / TEE_USER   SSH user on the SNP host (default: root). NB: the TEST
#                         itself always SSHes as root (tests reuse ssh_run, which
#                         hardcodes root@host); a non-root SNP host is an
#                         UNKNOWN to confirm — see ASSUMPTIONS.
#   SSH_KEY_FILE          SSH private key (default: ~/.ssh/id_ed25519)
#   SNP_STAGE_DIR         local staging dir (default: REPO_ROOT/.local/snp)
#   SNP_IMAGE_HOST_DIR    image location on the SNP host (default: /opt/aleph-snp-image)
#   ALEPH_VM_SRC          optional candidate-branch aleph-vm checkout, enables the
#                         local nix/cargo BUILD fallback when the staged
#                         artifacts are absent
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
BIN_DIR="$REPO_ROOT/bin"

SNP_HOST="${SNP_HOST:-${TEE_HOST:-}}"
if [ -z "$SNP_HOST" ]; then
    echo "ERROR: SNP_HOST (or TEE_HOST) must be set" >&2
    exit 1
fi
SNP_USER="${SNP_USER:-${TEE_USER:-root}}"
SSH_KEY_FILE="${SSH_KEY_FILE:-$HOME/.ssh/id_ed25519}"
SNP_STAGE_DIR="${SNP_STAGE_DIR:-$REPO_ROOT/.local/snp}"
SNP_IMAGE_HOST_DIR="${SNP_IMAGE_HOST_DIR:-/opt/aleph-snp-image}"
ALEPH_VM_SRC="${ALEPH_VM_SRC:-}"

SSH_OPTS=(-o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -i "$SSH_KEY_FILE")

snp_ssh() {
    local cmd="$1"
    if [ "$SNP_USER" = "root" ]; then
        ssh "${SSH_OPTS[@]}" "root@$SNP_HOST" "$cmd"
    else
        ssh "${SSH_OPTS[@]}" "$SNP_USER@$SNP_HOST" "sudo -n bash -c $(printf '%q' "$cmd")"
    fi
}

mkdir -p "$BIN_DIR" "$SNP_STAGE_DIR"

IMAGE_DIR="$SNP_STAGE_DIR/image"
CLI_STAGED="$SNP_STAGE_DIR/bin/aleph-attest-cli"

# --- optional local BUILD fallback -------------------------------------------
# Only runs when the workflow did NOT stage the artifacts and the operator
# pointed us at a candidate checkout. Kept deliberately simple; the CI path is
# RECEIVE.
if [ ! -x "$CLI_STAGED" ] && [ -n "$ALEPH_VM_SRC" ]; then
    if command -v cargo >/dev/null; then
        echo "==> Building aleph-attest-cli with cargo from $ALEPH_VM_SRC/rust ..."
        ( cd "$ALEPH_VM_SRC/rust" && cargo build --release -p aleph-attest-cli )
        mkdir -p "$SNP_STAGE_DIR/bin"
        cp "$ALEPH_VM_SRC/rust/target/release/aleph-attest-cli" "$CLI_STAGED"
    else
        echo "ERROR: cargo not found and no staged aleph-attest-cli at $CLI_STAGED" >&2
        exit 1
    fi
fi

if [ ! -d "$IMAGE_DIR" ] && [ -n "$ALEPH_VM_SRC" ]; then
    if command -v nix >/dev/null; then
        echo "==> Building the nix measured image from $ALEPH_VM_SRC (dir=nix#image) ..."
        # The flake lives at nix/ but references ../rust, so the repo root is the
        # flake source and dir=nix selects the sub-flake. Requires the checkout
        # to be a git repo with rust/ committed (git+file only sees tracked files).
        nix build "git+file://$ALEPH_VM_SRC?dir=nix#image" \
            --extra-experimental-features "nix-command flakes" \
            -o "$SNP_STAGE_DIR/image-result"
        # `-o` yields a symlink to a read-only store dir; copy into a writable
        # staging dir so scp and permission fixups work.
        rm -rf "$IMAGE_DIR"
        cp -rL "$SNP_STAGE_DIR/image-result" "$IMAGE_DIR"
        chmod -R u+rw "$IMAGE_DIR"
    else
        echo "ERROR: nix not found and no staged image at $IMAGE_DIR" >&2
        exit 1
    fi
fi

# --- validate staged artifacts -----------------------------------------------
if [ ! -x "$CLI_STAGED" ]; then
    echo "ERROR: aleph-attest-cli not staged at $CLI_STAGED." >&2
    echo "       The workflow must build it (cargo, candidate branch) and rsync" >&2
    echo "       it here, or set ALEPH_VM_SRC to build locally." >&2
    exit 1
fi
for f in bzImage initrd rootfs.ext4 rootfs.ext4.verity rootfs.ext4.roothash OVMF.fd measurement.hex; do
    if [ ! -f "$IMAGE_DIR/$f" ]; then
        echo "ERROR: measured image is missing $IMAGE_DIR/$f" >&2
        echo "       Expected the nix flake `image` output staged under $IMAGE_DIR." >&2
        exit 1
    fi
done

# --- 1. aleph-attest-cli: ./bin + SNP host -----------------------------------
cp "$CLI_STAGED" "$BIN_DIR/aleph-attest-cli"
chmod +x "$BIN_DIR/aleph-attest-cli"
# Smoke test on THIS host. It may fail if the runner-built binary's glibc is
# newer than ours (see ASSUMPTIONS); a failure here is informative, not fatal,
# because the test invokes the SNP-host copy, not this one.
"$BIN_DIR/aleph-attest-cli" --help >/dev/null 2>&1 \
    && echo "==> aleph-attest-cli runs on this host" \
    || echo "    WARNING: aleph-attest-cli --help failed on this host (glibc?). The test runs the SNP-host copy, so continuing."

echo "==> Copying aleph-attest-cli to the SNP host ($SNP_HOST:$SNP_IMAGE_HOST_DIR/aleph-attest-cli) ..."
snp_ssh "mkdir -p '$SNP_IMAGE_HOST_DIR'"
scp "${SSH_OPTS[@]}" "$CLI_STAGED" "$SNP_USER@$SNP_HOST:/tmp/aleph-attest-cli"
snp_ssh "mv /tmp/aleph-attest-cli '$SNP_IMAGE_HOST_DIR/aleph-attest-cli' && chmod +x '$SNP_IMAGE_HOST_DIR/aleph-attest-cli'"

# --- 2. Measurement: record where the test reads it --------------------------
measurement="$(tr -d '[:space:]' < "$IMAGE_DIR/measurement.hex")"
echo "$measurement" > "$SNP_STAGE_DIR/measurement"
echo "==> SNP launch measurement: $measurement"

# --- 3. Measured image -> SNP host (cache-keyed by measurement) --------------
# The image content is a pure function of the measured chain, so the measurement
# doubles as the cache key: if the SNP host already carries an image with this
# exact measurement, skip the (multi-hundred-MB) scp.
host_marker="$SNP_IMAGE_HOST_DIR/.measurement"
if snp_ssh "test -f '$host_marker' && [ \"\$(cat '$host_marker')\" = '$measurement' ]"; then
    echo "==> SNP host already has image for measurement $measurement (cache hit); skipping upload."
else
    echo "==> Uploading measured image to the SNP host ..."
    for f in bzImage initrd rootfs.ext4 rootfs.ext4.verity rootfs.ext4.roothash OVMF.fd measurement.hex; do
        scp "${SSH_OPTS[@]}" "$IMAGE_DIR/$f" "$SNP_USER@$SNP_HOST:/tmp/$f"
        snp_ssh "mv /tmp/$f '$SNP_IMAGE_HOST_DIR/$f'"
    done
    snp_ssh "echo '$measurement' > '$host_marker'"
fi

echo "==> SNP artifacts ready:"
echo "    client CLI (local):  $BIN_DIR/aleph-attest-cli"
echo "    client CLI (SNP):    $SNP_IMAGE_HOST_DIR/aleph-attest-cli"
echo "    measured image (SNP): $SNP_IMAGE_HOST_DIR/{bzImage,initrd,rootfs.ext4(+.verity/.roothash),OVMF.fd}"
echo "    measurement:         $SNP_STAGE_DIR/measurement ($measurement)"

# ── ASSUMPTIONS / UNKNOWNS (confirm before the first run) ────────────────────
# A1. aleph-attest-cli glibc: it is built on the GH runner (ubuntu-latest) and
#     executed on the SNP host. If the runner's glibc is newer than the SNP
#     host's, the SNP-host copy will fail to exec. Fix: build the CLI on the SNP
#     host (cargo) or via nix (musl/static), or on an older base. The local ./bin
#     smoke test above is only informative.
# A2. SNP host SSH user: this script honours SNP_USER, but tests/test_vm_snp.py
#     SSHes as root (ssh_run hardcodes root@). If AMD_SEV_SNP_USER is not root,
#     the test needs the same non-root+sudo treatment this script has.
# A3. nix `image` output layout: assumed to contain exactly bzImage, initrd,
#     rootfs.ext4, rootfs.ext4.verity, rootfs.ext4.roothash, OVMF.fd,
#     measurement.hex (matches nix/flake.nix `image` on the candidate branch at
#     authoring time). If the flake renames outputs, update the file list here,
#     in the workflow, and in tests/test_vm_snp.py together.
