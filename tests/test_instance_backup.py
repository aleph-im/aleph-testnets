"""Backup integration test: create → download → restore, validated with a
sentinel file. Uses its own VM because restore rewrites the rootfs.
"""
import uuid
from urllib.parse import urlsplit

import pytest

from tests.vm_helpers import (
    create_dispatched_instance,
    delete_instance,
    resolve_crn_address,
    ssh_run,
    wait_for_ssh,
)


@pytest.fixture(scope="module")
def backup_vm(aleph_cli, rootfs_hash, ssh_key_pair):
    """Own VM (restore rewrites the rootfs). Deleted on teardown so it doesn't
    hold CRN capacity for the rest of the run."""
    _, public_key_path = ssh_key_pair
    vm = create_dispatched_instance(
        aleph_cli, rootfs_hash, public_key_path, "backup-instance",
    )
    yield vm
    delete_instance(aleph_cli, vm.hash)


@pytest.mark.timeout(1800)
def test_instance_backup_restore(backup_vm, aleph_cli, ssh_key_pair, tmp_path):
    """Back up a VM, change a sentinel file, restore, and confirm the sentinel
    reverted — proving create/download/restore round-trips on-disk state."""
    private_key_path, _ = ssh_key_pair
    wait_for_ssh(private_key_path, backup_vm.crn_host, backup_vm.ssh_port, timeout=120)

    original = uuid.uuid4().hex
    ssh_run(
        private_key_path, backup_vm.crn_host, backup_vm.ssh_port,
        f"echo {original} > /root/sentinel.txt && sync",
    )

    # Create the backup (blocks until complete).
    aleph_cli("instance", "backup", "create", backup_vm.hash, "--follow", "--chain", "eth")

    # The backup metadata must be retrievable from the CRN control API.
    info = aleph_cli("instance", "backup", "info", backup_vm.hash, "--chain", "eth", parse_json=True)
    assert info, "backup info should return the created backup's metadata"

    # Download the archive. aleph-vm signs download_url with a hardcoded
    # https://<DOMAIN_NAME> origin (it assumes a TLS proxy on :443), which
    # testnet CRNs don't run — so the URL as returned is unreachable here.
    # The HMAC signature only covers backup_id:vm_hash:expires, NOT the
    # origin, so rewrite the URL onto the CRN's registered API origin and
    # hand it to the CLI, which accepts a presigned URL in place of a VM
    # hash. Drop this rewrite once aleph-vm builds the URL from the origin
    # the client actually used.
    download_url = info.get("download_url")
    assert download_url, f"backup info should include a download_url, got: {info}"
    parts = urlsplit(download_url)
    crn_api = resolve_crn_address(aleph_cli, backup_vm.crn_hash)
    reachable_url = f"{crn_api}{parts.path}?{parts.query}"

    archive = tmp_path / "backup.tar"
    aleph_cli("instance", "backup", "download", reachable_url, "-o", str(archive), "--chain", "eth")
    assert archive.exists() and archive.stat().st_size > 0, "Backup archive should be non-empty"

    # Mutate the sentinel after the backup, then restore from the archive.
    # The CRN just churned ~2GB of disk I/O (qcow2 backup + tar + download),
    # which can leave the guest unresponsive for a while — poll until SSH
    # answers again instead of giving it a single 15s attempt.
    wait_for_ssh(private_key_path, backup_vm.crn_host, backup_vm.ssh_port, timeout=180)
    modified = uuid.uuid4().hex
    ssh_run(
        private_key_path, backup_vm.crn_host, backup_vm.ssh_port,
        f"echo {modified} > /root/sentinel.txt && sync",
    )

    aleph_cli("instance", "backup", "restore", "--file", str(archive),
              backup_vm.hash, "--chain", "eth")

    # Restore restarts the VM; re-resolve placement and wait for SSH.
    backup_vm.refresh(aleph_cli, timeout=420)
    wait_for_ssh(private_key_path, backup_vm.crn_host, backup_vm.ssh_port, timeout=180)

    restored = ssh_run(
        private_key_path, backup_vm.crn_host, backup_vm.ssh_port,
        "cat /root/sentinel.txt",
    ).strip()
    assert restored == original, (
        f"Restore should revert the sentinel to {original!r}, got {restored!r}"
    )
