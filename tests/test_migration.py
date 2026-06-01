"""Integration test for VM migration between CRNs.

Flow: create instance → SSH on initial CRN + write a marker file → unlink that
CRN → wait for scheduler to reallocate → SSH on new CRN + verify the marker
survived (disk state preserved across migration).

Requires two CRNs provisioned and linked (CRN_COUNT=2), scheduler dispatch
enabled, and an Ubuntu rootfs image (ALEPH_TESTNET_ROOTFS).
"""
import uuid

import pytest

from tests.vm_helpers import (
    create_dispatched_instance,
    resolve_crn_host,
    ssh_run,
    wait_for_dispatched,
    wait_for_ssh,
)


@pytest.mark.timeout(900)
def test_instance_migration(aleph_cli, rootfs_hash, ssh_key_pair, crn_nodes):
    """End-to-end: create → SSH → unlink CRN → scheduler migrates → SSH new CRN."""
    private_key_path, public_key_path = ssh_key_pair

    # --- Phase 1: Create instance, verify on initial CRN ---
    vm = create_dispatched_instance(
        aleph_cli, rootfs_hash, public_key_path, "migration-instance",
    )
    wait_for_ssh(private_key_path, vm.crn_host, vm.ssh_port, timeout=60)

    # Persist a marker we'll verify after migration. `sync` flushes the page
    # cache before the VM is stopped by the unlink so the marker hits disk.
    marker = uuid.uuid4().hex
    ssh_run(
        private_key_path, vm.crn_host, vm.ssh_port,
        f"echo {marker} > /root/migration-marker.txt && sync",
    )

    # --- Phase 2: Unlink the initial CRN and verify migration ---
    aleph_cli("node", "unlink", "--crn", vm.crn_hash, "--chain", "eth")

    # Budget covers both the scheduler moving the allocation and the new CRN
    # booting the VM + mapping SSH.
    migrated = wait_for_dispatched(
        aleph_cli, vm.hash, timeout=540, different_from=vm.crn_hash,
    )
    new_crn_hash = migrated["placement"]["allocated_node"]
    assert new_crn_hash != vm.crn_hash, (
        f"Scheduler should migrate to a different CRN, but got {new_crn_hash}"
    )
    new_host = resolve_crn_host(aleph_cli, new_crn_hash)
    new_port = int(migrated["mapped_ports"]["22"])

    wait_for_ssh(private_key_path, new_host, new_port, timeout=60)

    persisted = ssh_run(
        private_key_path, new_host, new_port,
        "cat /root/migration-marker.txt",
    ).strip()
    assert persisted == marker, (
        f"Migration should preserve disk state — expected marker {marker!r} "
        f"but got {persisted!r}"
    )
