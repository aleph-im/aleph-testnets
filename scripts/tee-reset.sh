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
# The [6] bracket keeps the pattern from matching the remote shell's own
# cmdline (which contains the pattern string) — without it, pkill -f kills
# its parent shell and the ssh command exits 137 instead of 0.
remote "pkill -9 -f 'qemu-system-x8[6]' || true"
# Execution state (VM disks, sessions, sqlite DB) and download caches.
# /opt/aleph-ci-cache (encrypted-rootfs build cache) is intentionally kept —
# it is meant to survive across runs.
remote "rm -rf /var/lib/aleph/vm/* /var/cache/aleph/vm/*"
echo "==> Reset complete."
