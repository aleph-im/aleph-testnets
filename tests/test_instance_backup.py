"""Backup integration test: create → download → restore, validated with a
sentinel file. Uses its own VM because restore rewrites the rootfs.
"""
import uuid

import pytest

from tests.vm_helpers import create_dispatched_instance, ssh_run, wait_for_ssh


@pytest.fixture(scope="module")
def backup_vm(aleph_cli, rootfs_hash, ssh_key_pair):
    _, public_key_path = ssh_key_pair
    return create_dispatched_instance(
        aleph_cli, rootfs_hash, public_key_path, "backup-instance",
    )


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

    # Create the backup (blocks until complete) and download the archive.
    aleph_cli("instance", "backup", "create", backup_vm.hash, "--follow", "--chain", "eth")
    archive = tmp_path / "backup.tar"
    aleph_cli("instance", "backup", "download", backup_vm.hash, "-o", str(archive), "--chain", "eth")
    assert archive.exists() and archive.stat().st_size > 0, "Backup archive should be non-empty"

    # Mutate the sentinel after the backup, then restore from the archive.
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
