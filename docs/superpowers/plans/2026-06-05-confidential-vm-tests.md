# Confidential VM (AMD SEV) CI Test Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** End-to-end CI test of the confidential VM flow: encrypted rootfs → upload → confidential instance → scheduler placement → `confidential init-session`/`start` → SSH into the guest and verify SEV + dm-crypt rootfs.

**Architecture:** A static AMD SEV server (GitHub secrets `AMD_SEV_SNP_HOST`/`AMD_SEV_SNP_USER`) joins each PR run as CRN index 2 via marker files (`static`, `confidential`, `ssh-user`) that `crn-up.sh` understands. The encrypted rootfs is built and cached on that server; sevctl and the OVMF blob are fetched to the CCN droplet where pytest runs. A job-level concurrency group serializes runs on the single server.

**Tech Stack:** bash, GitHub Actions, pytest, aleph CLI ≥ 0.11.1, aleph-vm ≥ 1.12.0.

**Spec:** `docs/superpowers/specs/2026-06-05-confidential-vm-test-design.md` (approved).

**Repo facts the implementer needs:**
- Tests run **on the CCN droplet** (`scripts/local-up.sh --test` → pytest). The aleph CLI binary lives at `$REPO_ROOT/bin/aleph`; `run_tests` prepends `bin/` to PATH.
- CRN state lives in `.local/crn/<idx>/` files; `crn-up.sh --install/--register` loops `seq 0 $((CRN_COUNT-1))`.
- `tests/vm_helpers.py` has `poll`, `resolve_crn_host`, `wait_for_dispatched`, `wait_for_ssh`, `ssh_run`, `delete_instance`, and `NODESTATUS_ADDR`.
- The aleph-vm `.deb` ships `sevctl` at `/opt/sevctl`. The OVMF firmware is **not** in the `.deb`; the CRN downloads it by item hash from the instance message via the test CCN.
- Canonical firmware: item hash `ba5bb13f3abca960b101a759be162b229e2b7e93ecad9d1307e54de887f177ff`, blob sha256 `89b76b0e64fe9015084fbffdf8ac98185bafc688bfe7a0b398585c392d03c7ee`.
- pyaleph default `max_upload_file_size` is 1 GiB — too small for the ~3 GB encrypted image; the testnet CCN config must raise it.
- There is no unit-test infra in this repo (integration tests only). Verification per task = `bash -n` for shell, `python -m py_compile` + `pytest --collect-only` for Python, `python -c "yaml.safe_load"` for YAML. The real test is the CI run.

---

### Task 1: Bump aleph-cli to 0.11.1 in the manifesto

The `instance confidential` command tree landed in aleph-cli 0.11.1; main pins 0.11.0.

**Files:**
- Modify: `manifesto.yml`

- [ ] **Step 1: Edit the pin**

In `manifesto.yml`, replace:

```yaml
  aleph-cli:
    version: "0.11.0"
    url: "https://github.com/aleph-im/aleph-rs/releases/download/v0.11.0/aleph-cli-linux-x86_64"
```

with:

```yaml
  aleph-cli:
    version: "0.11.1"
    url: "https://github.com/aleph-im/aleph-rs/releases/download/v0.11.1/aleph-cli-linux-x86_64"
```

- [ ] **Step 2: Verify YAML parses**

Run: `python3 -c "import yaml; yaml.safe_load(open('manifesto.yml'))" && echo OK`
Expected: `OK`

- [ ] **Step 3: Commit**

```bash
git add manifesto.yml
git commit -m "chore: bump aleph-cli to 0.11.1 (instance confidential commands)"
```

---

### Task 2: Raise pyaleph upload limits for multi-GB rootfs

**Files:**
- Modify: `deploy/config.yml.tpl`

- [ ] **Step 1: Edit the storage section**

Replace:

```yaml
storage:
  store_files: true
  engine: filesystem
  folder: /var/lib/pyaleph
  max_file_size: 1073741824
  max_unauthenticated_upload_file_size: 1073741824
```

with:

```yaml
storage:
  store_files: true
  engine: filesystem
  folder: /var/lib/pyaleph
  # 4 GiB: the confidential VM test uploads a ~3 GB encrypted rootfs image.
  # max_upload_file_size is the authenticated-multipart limit the CLI's
  # `file upload` hits (pyaleph default: 1 GiB).
  max_file_size: 4294967296
  max_unauthenticated_upload_file_size: 4294967296
  max_upload_file_size: 4294967296
```

- [ ] **Step 2: Verify YAML parses**

Run: `python3 -c "import yaml; yaml.safe_load(open('deploy/config.yml.tpl'))" && echo OK`
Expected: `OK`

- [ ] **Step 3: Commit**

```bash
git add deploy/config.yml.tpl
git commit -m "chore: raise CCN upload limits to 4 GiB for confidential rootfs"
```

---

### Task 3: `scripts/tee-reset.sh` — reset a static CRN's aleph-vm state

Used by `crn-up.sh --install` (static path) and by the workflow's `if: always()` teardown (runner-side, works even if the CCN droplet is dead).

**Files:**
- Create: `scripts/tee-reset.sh`

- [ ] **Step 1: Write the script**

```bash
#!/usr/bin/env bash
# Reset the aleph-vm state of a static (persistent) CRN — e.g. the AMD SEV TEE
# server used by the confidential VM tests. Stops the supervisor, kills
# leftover QEMU processes and wipes execution/cache state so one CI run can't
# leak into the next. Idempotent; safe to run when aleph-vm was never
# installed.
#
# Usage: tee-reset.sh <host> [ssh-user] [ssh-key-file]
#   ssh-user defaults to root; a non-root user needs passwordless sudo.
set -euo pipefail

HOST="${1:?Usage: tee-reset.sh <host> [ssh-user] [ssh-key-file]}"
SSH_USER="${2:-root}"
SSH_KEY_FILE="${3:-$HOME/.ssh/id_ed25519}"

remote() {
    local cmd="$1"
    if [ "$SSH_USER" = "root" ]; then
        ssh -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null \
            -i "$SSH_KEY_FILE" "root@$HOST" "$cmd"
    else
        ssh -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null \
            -i "$SSH_KEY_FILE" "$SSH_USER@$HOST" "sudo -n bash -c $(printf '%q' "$cmd")"
    fi
}

echo "==> Resetting aleph-vm state on $HOST ..."
remote "systemctl stop aleph-vm-supervisor.service 2>/dev/null || true"
remote "pkill -9 -f qemu-system-x86 || true"
# Execution state (VM disks, sessions, sqlite DB) and download caches.
# /opt/aleph-ci-cache (encrypted-rootfs build cache) is intentionally kept —
# it is meant to survive across runs.
remote "rm -rf /var/lib/aleph/vm/* /var/cache/aleph/vm/*"
echo "==> Reset complete."
```

- [ ] **Step 2: Make executable, syntax-check**

Run: `chmod +x scripts/tee-reset.sh && bash -n scripts/tee-reset.sh && echo OK`
Expected: `OK`

- [ ] **Step 3: Commit**

```bash
git add scripts/tee-reset.sh
git commit -m "feat: add tee-reset.sh to wipe static CRN aleph-vm state between runs"
```

---

### Task 4: `crn-up.sh` — per-CRN ssh-user, static and confidential markers

**Files:**
- Modify: `scripts/crn-up.sh`

- [ ] **Step 1: Add state-file helpers after `crn_name()` (around line 153)**

Insert after the `crn_name()` function:

```bash
# Per-CRN optional state files (written by the caller, e.g. the CI workflow):
#   ssh-user       SSH user for this CRN (default: root). A non-root user
#                  needs passwordless sudo; remote commands run via `sudo -n`.
#   static         CRN not managed by this script's lifecycle: --provision and
#                  --destroy skip it; --install skips base system packages and
#                  resets aleph-vm state (tee-reset.sh) before installing.
#   confidential   Enable confidential computing (AMD SEV) in supervisor.env.
crn_ssh_user() {
    local f
    f="$(crn_dir "$1")/ssh-user"
    if [ -f "$f" ]; then cat "$f"; else echo "root"; fi
}

crn_is_static() {
    [ -f "$(crn_dir "$1")/static" ]
}

crn_is_confidential() {
    [ -f "$(crn_dir "$1")/confidential" ]
}
```

- [ ] **Step 2: Make `ssh_crn`/`scp_to_crn` user-aware**

Replace:

```bash
ssh_crn() {
    local idx="$1"; shift
    local ip
    ip=$(crn_ip "$idx")
    retry_255 ssh "${SSH_OPTS[@]}" -i "$SSH_KEY_FILE" "root@$ip" "$@"
}

scp_to_crn() {
    local idx="$1"; shift
    local ip
    ip=$(crn_ip "$idx")
    retry_255 scp "${SSH_OPTS[@]}" -i "$SSH_KEY_FILE" "$@" "root@$ip:/tmp/"
}
```

with:

```bash
ssh_crn() {
    local idx="$1"; shift
    local ip user
    ip=$(crn_ip "$idx")
    user=$(crn_ssh_user "$idx")
    if [ "$user" = "root" ]; then
        retry_255 ssh "${SSH_OPTS[@]}" -i "$SSH_KEY_FILE" "root@$ip" "$@"
    else
        # Non-root: run the remote command through passwordless sudo.
        retry_255 ssh "${SSH_OPTS[@]}" -i "$SSH_KEY_FILE" "$user@$ip" \
            "sudo -n bash -c $(printf '%q' "$*")"
    fi
}

scp_to_crn() {
    local idx="$1"; shift
    local ip user
    ip=$(crn_ip "$idx")
    user=$(crn_ssh_user "$idx")
    # /tmp is writable by any user; privileged moves happen via ssh_crn.
    retry_255 scp "${SSH_OPTS[@]}" -i "$SSH_KEY_FILE" "$@" "$user@$ip:/tmp/"
}
```

- [ ] **Step 3: Skip static CRNs in `provision()`**

At the top of the `for idx in $(seq 0 $((CRN_COUNT - 1)))` loop body in `provision()` (before `local dir`), insert:

```bash
        if crn_is_static "$idx"; then
            echo "==> CRN $idx is static — skipping provisioning."
            continue
        fi
```

- [ ] **Step 4: Skip static CRNs in `destroy()`**

In `destroy()`, after `[ -d "$dir" ] || continue`, insert:

```bash
        if [ -f "$dir/static" ]; then
            echo "==> Skipping static CRN state in $dir (not ours to destroy)."
            continue
        fi
```

- [ ] **Step 5: `install_crn` — confidential supervisor.env, /tmp-relayed copies, static reset**

In `install_crn()`, after the `crn-hash` block (the one appending `ALEPH_VM_NODE_HASH`) and before `# Copy config`, insert:

```bash
        # Confidential computing (AMD SEV) support
        if crn_is_confidential "$idx"; then
            cat >> "$env_file" <<EOF
ALEPH_VM_ENABLE_CONFIDENTIAL_COMPUTING=true
ALEPH_VM_SEV_CTL_PATH=/opt/sevctl
EOF
            echo "    Confidential computing: enabled"
        fi
```

Replace the config-copy block:

```bash
        # Copy config
        ssh_crn "$idx" "mkdir -p /etc/aleph-vm"
        retry_255 scp "${SSH_OPTS[@]}" \
            -i "$SSH_KEY_FILE" "$env_file" "root@$ip:/etc/aleph-vm/supervisor.env"
```

with (relay via /tmp so a non-root SSH user works):

```bash
        # Copy config (via /tmp: the SSH user may not be root)
        scp_to_crn "$idx" "$env_file"
        ssh_crn "$idx" "mkdir -p /etc/aleph-vm && mv /tmp/supervisor.env /etc/aleph-vm/supervisor.env"
```

Replace the apt block:

```bash
        # Wait for apt lock
        ssh_crn "$idx" "while fuser /var/lib/apt/lists/lock >/dev/null 2>&1; do sleep 2; done"

        # System update + dependencies
        echo "    Installing system packages..."
        # NEEDRESTART_SUSPEND stops needrestart from bouncing sshd after the
        # upgrade, which would reset the next SSH connection mid-handshake.
        ssh_crn "$idx" "NEEDRESTART_SUSPEND=1 DEBIAN_FRONTEND=noninteractive apt-get -o DPkg::Lock::Timeout=-1 update"
        ssh_crn "$idx" "NEEDRESTART_SUSPEND=1 DEBIAN_FRONTEND=noninteractive apt-get -o DPkg::Lock::Timeout=-1 upgrade -y"
        ssh_crn "$idx" "NEEDRESTART_SUSPEND=1 DEBIAN_FRONTEND=noninteractive apt-get -o DPkg::Lock::Timeout=-1 install -y docker.io apparmor-profiles"
```

with:

```bash
        if crn_is_static "$idx"; then
            # Static server: base packages are one-time provisioning
            # (docs/tee-server.md). Reset leftover aleph-vm state from
            # previous CI runs instead of upgrading the box.
            echo "    Static CRN: skipping base packages, resetting aleph-vm state..."
            "$REPO_ROOT/scripts/tee-reset.sh" "$ip" "$(crn_ssh_user "$idx")" "$SSH_KEY_FILE"
        else
            # Wait for apt lock
            ssh_crn "$idx" "while fuser /var/lib/apt/lists/lock >/dev/null 2>&1; do sleep 2; done"

            # System update + dependencies
            echo "    Installing system packages..."
            # NEEDRESTART_SUSPEND stops needrestart from bouncing sshd after the
            # upgrade, which would reset the next SSH connection mid-handshake.
            ssh_crn "$idx" "NEEDRESTART_SUSPEND=1 DEBIAN_FRONTEND=noninteractive apt-get -o DPkg::Lock::Timeout=-1 update"
            ssh_crn "$idx" "NEEDRESTART_SUSPEND=1 DEBIAN_FRONTEND=noninteractive apt-get -o DPkg::Lock::Timeout=-1 upgrade -y"
            ssh_crn "$idx" "NEEDRESTART_SUSPEND=1 DEBIAN_FRONTEND=noninteractive apt-get -o DPkg::Lock::Timeout=-1 install -y docker.io apparmor-profiles"
        fi
```

Replace the vm-connector block:

```bash
        # Start vm-connector
        echo "    Starting vm-connector..."
        ssh_crn "$idx" "docker pull $connector_image"
        ssh_crn "$idx" "docker run -d -p 127.0.0.1:4021:4021/tcp --restart=always --name vm-connector $connector_image" || \
            ssh_crn "$idx" "docker restart vm-connector" || true
```

with (recreate so a static server picks up image updates):

```bash
        # Start vm-connector. Remove any existing container first so a static
        # server picks up vm-connector image updates between runs.
        echo "    Starting vm-connector..."
        ssh_crn "$idx" "docker pull $connector_image"
        ssh_crn "$idx" "docker rm -f vm-connector 2>/dev/null || true"
        ssh_crn "$idx" "docker run -d -p 127.0.0.1:4021:4021/tcp --restart=always --name vm-connector $connector_image"
```

Replace the .deb upload branch:

```bash
        if [ -n "$local_deb" ]; then
            echo "    Uploading aleph-vm .deb..."
            retry_255 scp "${SSH_OPTS[@]}" \
                -i "$SSH_KEY_FILE" "$local_deb" "root@$ip:/opt/aleph-vm.deb"
        else
```

with:

```bash
        if [ -n "$local_deb" ]; then
            echo "    Uploading aleph-vm .deb..."
            scp_to_crn "$idx" "$local_deb"
            ssh_crn "$idx" "mv /tmp/$(basename "$local_deb") /opt/aleph-vm.deb"
        else
```

- [ ] **Step 6: Syntax-check**

Run: `bash -n scripts/crn-up.sh && echo OK`
Expected: `OK`

- [ ] **Step 7: Spot-check the sudo wrapping logic**

Run: `bash -c 'cmd="echo hi && echo there"; printf "sudo -n bash -c %q\n" "$cmd"'`
Expected output: `sudo -n bash -c echo\ hi\ &&\ echo\ there` — i.e. the whole command is one quoted argument to `bash -c`.

- [ ] **Step 8: Commit**

```bash
git add scripts/crn-up.sh
git commit -m "feat: support static/confidential CRNs with custom SSH user in crn-up.sh"
```

---

### Task 5: Vendor the encrypted-image build scripts

**Files:**
- Create: `scripts/confidential-image/build_debian_image.sh`
- Create: `scripts/confidential-image/setup_debian_rootfs.sh`

- [ ] **Step 1: Copy the scripts from the aleph-vm checkout at tag 1.12.0**

The local checkout `/home/olivier/git/aleph/aleph-vm` is at `1.12.0-19-g75b815d9` and `examples/example_confidential_image/` is **identical to tag 1.12.0** (verified with `git diff --stat 1.12.0 HEAD`). Copy:

```bash
mkdir -p scripts/confidential-image
cp /home/olivier/git/aleph/aleph-vm/examples/example_confidential_image/build_debian_image.sh scripts/confidential-image/
cp /home/olivier/git/aleph/aleph-vm/examples/example_confidential_image/setup_debian_rootfs.sh scripts/confidential-image/
chmod +x scripts/confidential-image/*.sh
```

- [ ] **Step 2: Add a provenance header to each file**

In **both** files, insert immediately after the shebang line:

```bash
#
# Vendored from aleph-im/aleph-vm @ 1.12.0
# (examples/example_confidential_image/<this filename>).
# These scripts define the encrypted-image layout the custom confidential
# OVMF expects (GPT: FAT32 /boot/efi + LUKS1 root, key delivered via QMP).
# When bumping aleph-vm in manifesto.yml, diff against upstream and re-sync.
# scripts/confidential-artifacts.sh hashes these files into its cache key,
# so any edit triggers a rebuild on the TEE server.
#
```

(Replace `<this filename>` with the actual file name in each copy.)

- [ ] **Step 3: Syntax-check both**

Run: `bash -n scripts/confidential-image/build_debian_image.sh && bash -n scripts/confidential-image/setup_debian_rootfs.sh && echo OK`
Expected: `OK`

- [ ] **Step 4: Commit**

```bash
git add scripts/confidential-image/
git commit -m "feat: vendor encrypted-image build scripts from aleph-vm 1.12.0"
```

---

### Task 6: `scripts/confidential-artifacts.sh` — sevctl, firmware, cached rootfs

Runs on the CCN droplet. Produces `$REPO_ROOT/bin/sevctl`, `.local/confidential/OVMF.fd`, `.local/confidential/rootfs.img`.

**Files:**
- Create: `scripts/confidential-artifacts.sh`

- [ ] **Step 1: Write the script**

```bash
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
SSH_KEY_FILE="${SSH_KEY_FILE:-$HOME/.ssh/id_ed25519}"
DISK_PASSWORD="${ALEPH_TESTNET_CONFIDENTIAL_PASSWORD:-test-password}"

# Canonical confidential OVMF blob on aleph.cloud. ITEM_HASH is the aleph
# storage item hash (used to download); SHA256 is the digest of the blob
# itself (what the SEV launch measurement is derived from).
FIRMWARE_ITEM_HASH="ba5bb13f3abca960b101a759be162b229e2b7e93ecad9d1307e54de887f177ff"
FIRMWARE_SHA256="89b76b0e64fe9015084fbffdf8ac98185bafc688bfe7a0b398585c392d03c7ee"
FIRMWARE_URL="https://api2.aleph.im/api/v0/storage/raw/${FIRMWARE_ITEM_HASH}"

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
echo "==> Fetching sevctl from the TEE server..."
scp "${SSH_OPTS[@]}" "$TEE_USER@$TEE_HOST:/opt/sevctl" "$BIN_DIR/sevctl"
chmod +x "$BIN_DIR/sevctl"

# --- 2. OVMF firmware ----------------------------------------------------------
firmware="$OUT_DIR/OVMF.fd"
if [ ! -f "$firmware" ] || ! echo "$FIRMWARE_SHA256  $firmware" | sha256sum -c --quiet 2>/dev/null; then
    echo "==> Downloading confidential OVMF firmware..."
    curl -fsSL -o "$firmware" "$FIRMWARE_URL"
    echo "$FIRMWARE_SHA256  $firmware" | sha256sum -c --quiet
fi
echo "    Firmware OK: $firmware"

# --- 3. Encrypted rootfs --------------------------------------------------------
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
```

- [ ] **Step 2: Make executable, syntax-check**

Run: `chmod +x scripts/confidential-artifacts.sh && bash -n scripts/confidential-artifacts.sh && echo OK`
Expected: `OK`

- [ ] **Step 3: Commit**

```bash
git add scripts/confidential-artifacts.sh
git commit -m "feat: add confidential-artifacts.sh (sevctl, OVMF, cached encrypted rootfs)"
```

---

### Task 7: `local-up.sh` — export confidential env vars in `run_tests`

**Files:**
- Modify: `scripts/local-up.sh` (the `run_tests()` function, around line 182)

- [ ] **Step 1: Add exports**

In `run_tests()`, after `export ALEPH_TESTNET_ROOTFS="$LOCAL_DIR/rootfs.img"` and before `cd "$REPO_ROOT"`, insert:

```bash
    # Confidential VM test artifacts (present only when CI prepared them via
    # scripts/confidential-artifacts.sh; tests/test_confidential.py skips
    # when these are unset).
    if [ -f "$LOCAL_DIR/confidential/rootfs.img" ]; then
        export ALEPH_TESTNET_CONFIDENTIAL_ROOTFS="$LOCAL_DIR/confidential/rootfs.img"
    fi
    if [ -f "$LOCAL_DIR/confidential/OVMF.fd" ]; then
        export ALEPH_TESTNET_CONFIDENTIAL_FIRMWARE="$LOCAL_DIR/confidential/OVMF.fd"
    fi
    # TEE host: first CRN state dir carrying the `confidential` marker.
    local crn_state_dir
    for crn_state_dir in "$LOCAL_DIR"/crn/*/; do
        if [ -f "$crn_state_dir/confidential" ] && [ -f "$crn_state_dir/droplet-ip" ]; then
            export ALEPH_TESTNET_CONFIDENTIAL_CRN_HOST="$(cat "$crn_state_dir/droplet-ip")"
            break
        fi
    done
    export ALEPH_TESTNET_CONFIDENTIAL_PASSWORD="${ALEPH_TESTNET_CONFIDENTIAL_PASSWORD:-test-password}"
```

- [ ] **Step 2: Syntax-check**

Run: `bash -n scripts/local-up.sh && echo OK`
Expected: `OK`

- [ ] **Step 3: Commit**

```bash
git add scripts/local-up.sh
git commit -m "feat: export confidential test env vars in local-up.sh run_tests"
```

---

### Task 8: `vm_helpers.wait_for_dispatched` — placement-only mode

Confidential VMs are placed by the scheduler *before* they start; port 22 is only mapped after secret injection. `required_port=None` must mean "wait for placement only".

**Files:**
- Modify: `tests/vm_helpers.py` (function `wait_for_dispatched`)

- [ ] **Step 1: Edit the function**

Replace the body of `wait_for_dispatched` with:

```python
def wait_for_dispatched(aleph_cli, vm_hash, timeout=300, *, different_from=None, required_port="22"):
    """Poll `instance show --verbose` until the VM is dispatched with the named
    host-side mapped port present. If `different_from` is set, also require the
    allocated node hash to differ (used to detect post-unlink migration).
    `required_port=None` waits for placement only — confidential VMs are placed
    before they start, and ports are only mapped after secret injection."""
    def fetch():
        data = aleph_cli(
            "instance", "show", vm_hash, "--verbose",
            parse_json=True, check=False,
        )
        if not isinstance(data, dict):
            return None
        node = data.get("placement", {}).get("allocated_node")
        if not node:
            return None
        if different_from is not None and node == different_from:
            return None
        if required_port is not None:
            mapped = data.get("mapped_ports") or {}
            if mapped.get(required_port) is None:
                return None
        return data
    label = f"VM {vm_hash[:12]} dispatched"
    if required_port is not None:
        label += f" + port {required_port}"
    if different_from is not None:
        label += f" (different from {different_from[:12]})"
    return poll(label, fetch, timeout=timeout)
```

- [ ] **Step 2: Compile + collect**

Run: `python3 -m py_compile tests/vm_helpers.py && python3 -m pytest tests/ --collect-only -q | tail -3`
Expected: compiles; collection lists the existing tests with no errors.

- [ ] **Step 3: Commit**

```bash
git add tests/vm_helpers.py
git commit -m "feat: wait_for_dispatched placement-only mode for confidential VMs"
```

---

### Task 9: conftest — confidential fixtures

**Files:**
- Modify: `tests/conftest.py` (append after the `rootfs_hash` fixture)

- [ ] **Step 1: Add fixtures**

```python
@pytest.fixture(scope="session")
def confidential_rootfs() -> str:
    path = os.environ.get("ALEPH_TESTNET_CONFIDENTIAL_ROOTFS", "")
    if not path or not os.path.exists(path):
        pytest.skip("No confidential rootfs — requires ALEPH_TESTNET_CONFIDENTIAL_ROOTFS")
    return path


@pytest.fixture(scope="session")
def confidential_firmware() -> str:
    path = os.environ.get("ALEPH_TESTNET_CONFIDENTIAL_FIRMWARE", "")
    if not path or not os.path.exists(path):
        pytest.skip("No confidential firmware — requires ALEPH_TESTNET_CONFIDENTIAL_FIRMWARE")
    return path


@pytest.fixture(scope="session")
def confidential_crn_host() -> str:
    """Address of the static TEE server (the only confidential-capable CRN)."""
    host = os.environ.get("ALEPH_TESTNET_CONFIDENTIAL_CRN_HOST", "")
    if not host:
        pytest.skip("No TEE server — requires ALEPH_TESTNET_CONFIDENTIAL_CRN_HOST")
    return host


@pytest.fixture(scope="session")
def confidential_password() -> str:
    """Disk-decryption password baked into the encrypted rootfs by
    scripts/confidential-artifacts.sh. A fixed, non-secret test value —
    the default must match that script's."""
    return os.environ.get("ALEPH_TESTNET_CONFIDENTIAL_PASSWORD", "test-password")


@pytest.fixture(scope="session")
def confidential_rootfs_hash(aleph_cli, confidential_rootfs) -> str:
    """Upload the encrypted rootfs once per session; return its item_hash."""
    result = aleph_cli(
        "file", "upload", confidential_rootfs,
        "--storage-engine", "storage", "--chain", "eth",
        parse_json=True,
    )
    item_hash = result["item_hash"]
    assert item_hash, "Confidential rootfs upload should return an item_hash"
    return item_hash


@pytest.fixture(scope="session")
def confidential_firmware_hash(aleph_cli, confidential_firmware) -> str:
    """Upload the OVMF blob once per session; return its item_hash.

    Must be referenced explicitly at create time: the CLI's default firmware
    resolution reads the `vm-images` aggregate, which does not exist on a
    fresh testnet CCN, and the CRN downloads the firmware by this hash."""
    result = aleph_cli(
        "file", "upload", confidential_firmware,
        "--storage-engine", "storage", "--chain", "eth",
        parse_json=True,
    )
    item_hash = result["item_hash"]
    assert item_hash, "Firmware upload should return an item_hash"
    return item_hash
```

- [ ] **Step 2: Compile + collect**

Run: `python3 -m py_compile tests/conftest.py && python3 -m pytest tests/ --collect-only -q | tail -3`
Expected: no errors.

- [ ] **Step 3: Commit**

```bash
git add tests/conftest.py
git commit -m "feat: add confidential VM test fixtures"
```

---

### Task 10: `tests/test_confidential.py`

**Files:**
- Create: `tests/test_confidential.py`

- [ ] **Step 1: Write the test**

```python
"""End-to-end confidential VM (AMD SEV) test.

Flow: upload encrypted rootfs + OVMF firmware → create a confidential
instance → scheduler places it on the TEE server → `confidential
init-session` (cert chain verification + session keys via sevctl) →
`confidential start` (launch-measurement validation + disk secret
injection) → SSH in → verify SEV is active and the rootfs is dm-crypt.

Requires the static TEE server configured as a confidential CRN (see
docs/tee-server.md) and the artifacts prepared by
scripts/confidential-artifacts.sh. All `ALEPH_TESTNET_CONFIDENTIAL_*`
fixtures skip when unset, so local runs without a TEE server still pass.
"""
import pytest

from tests.vm_helpers import (
    NODESTATUS_ADDR,
    delete_instance,
    poll,
    resolve_crn_host,
    ssh_run,
    wait_for_dispatched,
    wait_for_ssh,
)


@pytest.mark.timeout(900)
def test_confidential_instance_create_and_ssh(
    aleph_cli,
    confidential_rootfs_hash,
    confidential_firmware_hash,
    confidential_firmware,
    confidential_password,
    confidential_crn_host,
    ssh_key_pair,
):
    private_key_path, public_key_path = ssh_key_pair

    # Fail fast if the TEE CRN is not linked in the corechannel aggregate
    # (e.g. unlinked by the migration test without re-linking) — placement
    # can never happen then, and this beats a 300 s poll timeout.
    nodes = aleph_cli(
        "node", "list", "--type", "crn", "--all",
        "--corechannel-address", NODESTATUS_ADDR,
        parse_json=True,
    ) or []
    assert any(confidential_crn_host in n.get("address", "") for n in nodes), (
        f"TEE CRN {confidential_crn_host} is not linked in the corechannel "
        "aggregate; confidential instances cannot be scheduled."
    )

    result = aleph_cli(
        "instance", "create", "test-confidential",
        "--image", confidential_rootfs_hash,
        "--confidential",
        "--confidential-firmware", confidential_firmware_hash,
        "--vcpus", "1",
        "--memory", "4GiB",
        "--disk-size", "4GiB",
        "--ssh-pubkey-file", public_key_path,
        "--chain", "eth",
        parse_json=True,
    )
    vm_hash = result["item_hash"]
    assert vm_hash, "Instance create should return an item_hash"

    try:
        # Scheduler placement. required_port=None: a confidential VM is
        # placed before it starts; ports map only after secret injection.
        data = wait_for_dispatched(aleph_cli, vm_hash, timeout=300, required_port=None)
        crn_hash = data["placement"]["allocated_node"]
        crn_host = resolve_crn_host(aleph_cli, crn_hash)
        assert crn_host == confidential_crn_host, (
            f"Confidential instance landed on {crn_host}, expected the TEE "
            f"server {confidential_crn_host}"
        )

        # Init sequence: fetch + verify the platform cert against AMD roots,
        # derive session keys (sevctl), post them to the CRN.
        aleph_cli("instance", "confidential", "init-session", vm_hash, "--keep-session")

        # Start: validate the launch measurement against the known firmware
        # and inject the disk-decryption secret. The standalone `start` does
        # not poll for measurement-readiness, so retry until the CRN reports
        # the measurement (the VM needs a moment to boot to that point).
        def try_start():
            r = aleph_cli(
                "instance", "confidential", "start", vm_hash,
                "--secret", confidential_password,
                "--firmware-file", confidential_firmware,
                "--json",
                check=False,
            )
            if r.returncode == 0:
                return r
            raise RuntimeError(f"confidential start: {r.stderr.strip()[-500:]}")

        poll("Confidential start (measurement + secret injection)", try_start,
             timeout=300, interval=10)

        # The guest now decrypts its disk and boots; wait for SSH.
        data = wait_for_dispatched(aleph_cli, vm_hash, timeout=300)
        ssh_port = int(data["mapped_ports"]["22"])
        wait_for_ssh(private_key_path, crn_host, ssh_port, timeout=120)

        # TEE verification — the point of this test.
        # 1. The guest kernel reports SEV memory encryption active.
        dmesg = ssh_run(
            private_key_path, crn_host, ssh_port,
            "dmesg | grep -i 'Memory Encryption Features active' || true",
        )
        assert "SEV" in dmesg, f"Guest kernel does not report SEV active: {dmesg!r}"

        # 2. The root filesystem is on a dm-crypt mapping (the encrypted
        #    rootfs actually engaged, rather than some fallback plain disk).
        root_src = ssh_run(
            private_key_path, crn_host, ssh_port, "findmnt -no SOURCE /",
        ).strip()
        assert root_src.startswith("/dev/mapper/"), (
            f"Root filesystem is not dm-crypt mapped: {root_src!r}"
        )
    finally:
        delete_instance(aleph_cli, vm_hash)
```

- [ ] **Step 2: Compile + collect**

Run: `python3 -m py_compile tests/test_confidential.py && python3 -m pytest tests/test_confidential.py --collect-only -q`
Expected: `tests/test_confidential.py::test_confidential_instance_create_and_ssh` collected, no errors.

- [ ] **Step 3: Commit**

```bash
git add tests/test_confidential.py
git commit -m "test: add end-to-end confidential VM (AMD SEV) test"
```

---

### Task 11: `test_migration.py` — re-link the unlinked CRN in teardown

**Files:**
- Modify: `tests/test_migration.py`

- [ ] **Step 1: Edit the `finally` block**

Replace:

```python
    finally:
        delete_instance(aleph_cli, vm.hash)
```

with:

```python
    finally:
        # Re-link the CRN this test unlinked: the corechannel aggregate is
        # shared state, and the static TEE CRN in particular must stay linked
        # for the confidential test. Best-effort, like delete_instance.
        aleph_cli("node", "link", "--crn", vm.crn_hash, "--chain", "eth", check=False)
        delete_instance(aleph_cli, vm.hash)
```

Note: `vm.crn_hash` is the *initial* CRN (the one unlinked in phase 2) — `DispatchedVM.refresh` is not called in this test, so the attribute still holds the unlinked node's hash.

- [ ] **Step 2: Compile + collect**

Run: `python3 -m py_compile tests/test_migration.py && python3 -m pytest tests/test_migration.py --collect-only -q`
Expected: no errors.

- [ ] **Step 3: Commit**

```bash
git add tests/test_migration.py
git commit -m "fix: re-link unlinked CRN after migration test (shared corechannel state)"
```

---

### Task 12: `pr-tests.yml` — wire the TEE server into the workflow

**Files:**
- Modify: `.github/workflows/pr-tests.yml`

- [ ] **Step 1: Job concurrency + env counts**

Replace:

```yaml
  integration-tests:
    runs-on: ubuntu-latest
    timeout-minutes: 60
    environment: digitalocean
    env:
      # Number of CRN droplets to provision. The migration test requires 2.
      CRN_COUNT: 2
```

with:

```yaml
  integration-tests:
    runs-on: ubuntu-latest
    timeout-minutes: 60
    environment: digitalocean
    # Serialize across ALL runs (not just per-PR): the static TEE server can
    # only host one CI run at a time. Caveat: GitHub keeps a single pending
    # run per group — with 3+ queued runs, intermediate ones are cancelled.
    concurrency:
      group: amd-sev-snp-server
      cancel-in-progress: false
    env:
      # DigitalOcean CRN droplets to provision. The migration test requires 2.
      DO_CRN_COUNT: 2
      # Total CRNs, including the static TEE server at index 2 (= DO_CRN_COUNT).
      CRN_COUNT: 3
```

- [ ] **Step 2: Point droplet loops at `DO_CRN_COUNT`**

Change `seq 0 $((CRN_COUNT - 1))` to `seq 0 $((DO_CRN_COUNT - 1))` in these steps (droplet lifecycle only):
- `Create CCN and CRN Droplets`
- `Get Droplet IPs`
- `Wait for SSH`
- `Install system dependencies (CCN + CRN in parallel)`
- `Collect CRN logs`
- `Destroy CRN Droplets`

Leave `CRN_COUNT` untouched in `Install and register CRNs` (the crn-up.sh invocations must cover index 2) — but the state-writing loop at the top of that step iterates droplets, so change *that* loop to `DO_CRN_COUNT` too (the TEE state is written separately in Step 4 below).

- [ ] **Step 3: Add the TEE host to known_hosts**

At the end of the `Wait for SSH` step, append:

```yaml
          # Static TEE server: already running, just trust its key.
          ssh-keyscan -H "${{ secrets.AMD_SEV_SNP_HOST }}" >> ~/.ssh/known_hosts 2>/dev/null || true
```

- [ ] **Step 4: Write TEE CRN state in `Install and register CRNs`**

In the `Install and register CRNs` step, after the per-droplet state-writing loop and before the `# Copy SSH key to CCN droplet` comment, insert:

```bash
          # Static TEE server as CRN index ${DO_CRN_COUNT} (see docs/tee-server.md):
          # static = don't provision/destroy/upgrade; confidential = enable
          # AMD SEV in supervisor.env; ssh-user = non-root login support.
          TEE_IDX=${DO_CRN_COUNT}
          ssh root@${DROPLET_IPV4} "mkdir -p /opt/aleph-testnets/.local/crn/${TEE_IDX}"
          ssh root@${DROPLET_IPV4} "echo 'amd-sev-snp-server' > /opt/aleph-testnets/.local/crn/${TEE_IDX}/droplet-name"
          ssh root@${DROPLET_IPV4} "echo '${{ secrets.AMD_SEV_SNP_HOST }}' > /opt/aleph-testnets/.local/crn/${TEE_IDX}/droplet-ip"
          ssh root@${DROPLET_IPV4} "echo '${{ secrets.AMD_SEV_SNP_USER }}' > /opt/aleph-testnets/.local/crn/${TEE_IDX}/ssh-user"
          ssh root@${DROPLET_IPV4} "touch /opt/aleph-testnets/.local/crn/${TEE_IDX}/static /opt/aleph-testnets/.local/crn/${TEE_IDX}/confidential"
```

(The existing `crn-up.sh --install` / `--register` invocations already pass `CRN_COUNT=${CRN_COUNT}` = 3, so they pick up index 2 automatically.)

- [ ] **Step 5: Add the artifacts step after `Install and register CRNs`**

```yaml
      - name: Prepare confidential VM artifacts
        run: |
          ssh root@${DROPLET_IPV4} "cd /opt/aleph-testnets && \
            TEE_HOST='${{ secrets.AMD_SEV_SNP_HOST }}' \
            TEE_USER='${{ secrets.AMD_SEV_SNP_USER }}' \
            SSH_KEY_FILE=/root/.ssh/id_ed25519 \
            bash scripts/confidential-artifacts.sh"
```

- [ ] **Step 6: Collect TEE server logs**

After the `Collect CRN logs` step, add:

```yaml
      - name: Collect TEE server logs
        if: always()
        run: |
          TEE_HOST="${{ secrets.AMD_SEV_SNP_HOST }}"
          TEE_USER="${{ secrets.AMD_SEV_SNP_USER }}"
          if [ "$TEE_USER" = "root" ]; then
            ssh "root@$TEE_HOST" "journalctl -u aleph-vm-supervisor.service --no-pager -n 500" > tee-supervisor.txt 2>&1 || true
          else
            ssh "$TEE_USER@$TEE_HOST" "sudo -n journalctl -u aleph-vm-supervisor.service --no-pager -n 500" > tee-supervisor.txt 2>&1 || true
          fi
```

And add `tee-supervisor.txt` to the `Upload artifacts` step's `path:` list.

- [ ] **Step 7: Reset the TEE server in teardown**

After the `Upload artifacts` step (before `Destroy CRN Droplets`), add:

```yaml
      - name: Reset TEE server
        if: always()
        run: |
          bash scripts/tee-reset.sh "${{ secrets.AMD_SEV_SNP_HOST }}" "${{ secrets.AMD_SEV_SNP_USER }}" ~/.ssh/id_ed25519 || true
```

- [ ] **Step 8: Validate YAML**

Run: `python3 -c "import yaml; yaml.safe_load(open('.github/workflows/pr-tests.yml'))" && echo OK`
Expected: `OK`

- [ ] **Step 9: Commit**

```bash
git add .github/workflows/pr-tests.yml
git commit -m "ci: add static AMD SEV TEE server as third CRN for confidential tests"
```

---

### Task 13: `docs/tee-server.md` — one-time provisioning doc

**Files:**
- Create: `docs/tee-server.md`

- [ ] **Step 1: Write the doc**

```markdown
# TEE server (AMD SEV) — one-time provisioning

A single static server provides the confidential-computing CRN for the CI
test suite (`tests/test_confidential.py`). CI does **not** create or destroy
it; everything below is one-time manual setup. Its coordinates live in the
GitHub Actions secrets `AMD_SEV_SNP_HOST` and `AMD_SEV_SNP_USER` (plain
Actions secrets, not part of the `digitalocean` environment), and CI
authenticates with the same SSH key as the DigitalOcean droplets
(`DIGITALOCEAN_SSH_PRIVATE_KEY`).

## Requirements

- **BIOS/firmware**: AMD SEV and SEV-ES enabled, with sufficient SEV ASIDs.
- **Kernel**: KVM AMD SEV active — `cat /sys/module/kvm_amd/parameters/sev`
  must print `Y` (and `sev_es` likewise for SEV-ES).
- **OS packages** (Debian 12 assumed):
  `apt install docker.io apparmor-profiles sudo guestmount parted cryptsetup`
  - `docker.io`, `apparmor-profiles`: standard CRN dependencies.
  - `sudo`: required even for root logins — the vendored image build script
    invokes `sudo` internally.
  - `guestmount` (libguestfs-tools), `parted`, `cryptsetup`: encrypted
    rootfs build dependencies.
- **SSH access**: the CI key accepted for `$AMD_SEV_SNP_USER`. If that user
  is not `root`, it needs **passwordless sudo** (`NOPASSWD:ALL`).
- **Network**: reachable from the CI-created DigitalOcean droplets —
  inbound TCP 4020 (CRN supervisor) and the high ports aleph-vm maps for VM
  SSH forwarding. No NAT/firewall rules that would block them.
- `sevctl` is **not** a manual prerequisite: the aleph-vm `.deb` installs it
  at `/opt/sevctl` on every CI run.

## What CI does to this server on every run

Treat the box as disposable from the neck up — do not run anything stateful
on it:

- `scripts/tee-reset.sh`: stops `aleph-vm-supervisor`, kills leftover QEMU
  processes, wipes `/var/lib/aleph/vm/*` and `/var/cache/aleph/vm/*`
  (at install time and again in `if: always()` teardown).
- `scripts/crn-up.sh --install`: writes `/etc/aleph-vm/supervisor.env`
  (pointing at the run's fresh CCN, with
  `ALEPH_VM_ENABLE_CONFIDENTIAL_COMPUTING=true`), recreates the
  `vm-connector` container, installs the manifesto-pinned aleph-vm `.deb`,
  restarts the supervisor.
- `scripts/crn-up.sh --register`: registers it as a CRN in the run's
  corechannel aggregate (which dies with the run's CCN droplet).
- `scripts/confidential-artifacts.sh`: builds/caches the encrypted test
  rootfs under `/opt/aleph-ci-cache/` (created automatically; the only
  state that intentionally survives runs).

## Concurrency

The `integration-tests` job carries a `concurrency: amd-sev-snp-server`
group so only one CI run touches the server at a time. GitHub keeps a single
pending run per group; if more stack up, intermediate ones are cancelled.
Manual use of the server while CI runs is not protected against.
```

- [ ] **Step 2: Commit**

```bash
git add docs/tee-server.md
git commit -m "docs: TEE server one-time provisioning guide"
```

---

### Task 14: Final verification sweep

**Files:** none (verification only)

- [ ] **Step 1: Shell syntax across all touched scripts**

Run: `for f in scripts/*.sh scripts/confidential-image/*.sh; do bash -n "$f" || echo "FAIL: $f"; done; echo DONE`
Expected: `DONE` with no `FAIL:` lines.

- [ ] **Step 2: shellcheck (advisory, if installed)**

Run: `command -v shellcheck >/dev/null && shellcheck -S warning scripts/tee-reset.sh scripts/confidential-artifacts.sh scripts/crn-up.sh || echo "shellcheck not available — skipped"`
Expected: no *new* warnings in the added code (pre-existing warnings in crn-up.sh are out of scope).

- [ ] **Step 3: Python compile + full collection**

Run: `python3 -m py_compile tests/*.py && python3 -m pytest tests/ --collect-only -q | tail -5`
Expected: all test modules collect; `test_confidential_instance_create_and_ssh` listed.

- [ ] **Step 4: YAML sanity**

Run: `python3 -c "import yaml; [yaml.safe_load(open(f)) for f in ['manifesto.yml', 'deploy/config.yml.tpl', '.github/workflows/pr-tests.yml']]" && echo OK`
Expected: `OK`

- [ ] **Step 5: Review the full diff against the spec**

Run: `git log --oneline origin/main..HEAD && git diff origin/main --stat`
Check each spec section maps to a commit. The real end-to-end validation is the CI run on the PR — open the PR and watch the `integration-tests` job (the first run pays the ~5-10 min rootfs build; later runs hit the cache).

---

## Known risks / debugging notes for the executor

- **First CI run is the integration test of this plan.** Likely failure points, in order: TEE server SSH/sudo access; the scheduler not advertising the TEE CRN as confidential (check `http://<tee>:4020/about/config` exposes `ENABLE_CONFIDENTIAL_COMPUTING: true`); measurement mismatch in `confidential start` (would indicate the CRN's loaded firmware ≠ the uploaded blob — verify the instance message's `trusted_execution.firmware` hash matches the uploaded item hash); cloud-init not applying the SSH key inside the encrypted guest.
- The aleph CLI needs `sevctl` on PATH **where pytest runs** — `run_tests` prepends `$REPO_ROOT/bin`, where `confidential-artifacts.sh` puts it.
- `aleph instance create --confidential` uses the confidential pricing tier; if creation fails with insufficient credits, raise the funding in `scripts/fund-test-accounts.sh` (out of scope here, noted for debugging).
- The instance-create `--disk-size 4GiB` must be ≥ the 3 GB image size.
```
