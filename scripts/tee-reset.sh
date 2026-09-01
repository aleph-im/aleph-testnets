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
# Per-VM controller units outlive the state wipe below: systemd keeps
# restarting a launcher whose controller.json is gone (observed at a restart
# counter of 844k, flooding the journal and drowning the CI log capture).
# Stop and clear every controller unit so none leaks into the next run.
remote "systemctl stop 'aleph-vm-controller@*' 2>/dev/null || true"
remote "systemctl reset-failed 'aleph-vm-controller@*' 2>/dev/null || true"
# Per-tap DHCP units (dnsmasq) hold the tap devices open and linger after
# their VM is gone; stop them before deleting the taps below.
remote "systemctl stop 'aleph-vm-dhcp-*' 2>/dev/null || true"
remote "systemctl reset-failed 'aleph-vm-dhcp-*' 2>/dev/null || true"
# The [6] bracket keeps the pattern from matching the remote shell's own
# cmdline (which contains the pattern string) — without it, pkill -f kills
# its parent shell and the ssh command exits 137 instead of 0.
remote "pkill -9 -f 'qemu-system-x8[6]' || true"
# Execution state (VM disks, sessions, sqlite DB) and download caches.
# /opt/aleph-ci-cache (encrypted-rootfs build cache) is intentionally kept —
# it is meant to survive across runs.
remote "rm -rf /var/lib/aleph/vm/* /var/cache/aleph/vm/*"
# Leftover vmtap* links: killing QEMU leaves the taps behind (NO-CARRIER)
# with their per-VM IPv4/IPv6 pool addresses still configured. The next
# run's supervisor starts its slice allocator from scratch and re-assigns
# the same IPv6 /124 to a fresh tap, leaving two interfaces owning the
# prefix; the kernel keeps using the dead tap's route (linkdown routes are
# not ignored by default), blackholing the new guest's IPv6 (run
# 33512021462: host's `ip -6 route get <guest-v6>` resolved to the dead
# tap). IPv4 never collides because its pool index keeps advancing.
remote "for t in \$(ip -o link show | awk -F': ' '/^[0-9]+: vmtap/{print \$2}'); do ip link del \"\$t\" || true; done"
echo "==> Reset complete."
