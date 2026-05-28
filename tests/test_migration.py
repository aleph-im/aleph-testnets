"""Integration test for VM migration between CRNs.

Flow: create instance → SSH on initial CRN + write a marker file → unlink that
CRN → wait for scheduler to reallocate → SSH on new CRN + verify the marker
survived (disk state preserved across migration).

Discovery, SSH, and node operations all go through the CLI: `aleph instance
show --verbose` (scheduler placement + CRN networking + mapped ports),
`aleph instance ssh` (IPv6), `aleph node unlink`.

Requires:
- Two CRNs provisioned and linked (CRN_COUNT=2)
- Scheduler-rs with dispatch enabled
- Ubuntu rootfs image (ALEPH_TESTNET_ROOTFS)
"""
import time
import uuid

import pytest


def _poll(description, fetch, timeout, interval=5):
    """Poll fetch() until it returns a truthy value or timeout is reached."""
    deadline = time.time() + timeout
    last_err = None
    while time.time() < deadline:
        try:
            result = fetch()
            if result is not None:
                return result
        except Exception as e:
            last_err = e
        time.sleep(interval)
    pytest.fail(f"{description} did not succeed within {timeout}s (last error: {last_err})")


def _vm_ssh_run(aleph_cli, vm_hash, identity, remote_cmd):
    """Run a single remote command on the VM via `aleph instance ssh`.

    The command string is passed as one positional after `--`, so the remote
    login shell parses any pipes/redirections.
    """
    result = aleph_cli(
        "instance", "ssh", vm_hash,
        "--identity", identity,
        "--", remote_cmd,
    )
    return result.stdout


def _wait_for_ssh(aleph_cli, vm_hash, identity, timeout=60):
    """Poll `aleph instance ssh` until 'echo hello' succeeds on the VM."""
    def try_ssh():
        result = aleph_cli(
            "instance", "ssh", vm_hash,
            "--identity", identity,
            "--", "echo", "hello",
            check=False,
        )
        if result.returncode == 0 and "hello" in result.stdout:
            return result.stdout.strip()
        return None
    return _poll(f"SSH into VM {vm_hash[:12]}", try_ssh, timeout=timeout)


def _wait_for_dispatched(aleph_cli, vm_hash, timeout=300, *, different_from=None):
    """Poll `aleph instance show --verbose` until the VM is dispatched with a
    mapped SSH port. If `different_from` is set, also require the allocated
    node hash to differ from that value (used to detect post-unlink migration).
    """
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
        mapped = data.get("mapped_ports") or {}
        if mapped.get("22") is None:
            return None
        return data
    label = f"VM {vm_hash[:12]} dispatched + SSH port"
    if different_from is not None:
        label += f" (different from {different_from[:12]})"
    return _poll(label, fetch, timeout=timeout)


@pytest.mark.timeout(900)
def test_instance_migration(aleph_cli, rootfs_image, ssh_key_pair, crn_nodes):
    """End-to-end: create instance → SSH → unlink CRN → scheduler migrates → SSH on new CRN."""
    private_key_path, public_key_path = ssh_key_pair

    # --- Phase 1: Create instance and verify on initial CRN ---

    upload_result = aleph_cli(
        "file", "upload", rootfs_image, "--storage-engine", "storage", "--chain", "eth", parse_json=True
    )
    rootfs_hash = upload_result["item_hash"]
    assert rootfs_hash, "Upload should return an item_hash"

    instance_result = aleph_cli(
        "instance", "create",
        "migration-instance",
        "--image", rootfs_hash,
        "--disk-size", "4GiB",
        "--ssh-pubkey-file", public_key_path,
        "--vcpus", "1",
        "--memory", "2GiB",
        "--chain", "eth",
        parse_json=True,
    )
    instance_hash = instance_result["item_hash"]
    assert instance_hash, "Instance create should return an item_hash"

    initial = _wait_for_dispatched(aleph_cli, instance_hash, timeout=300)
    initial_crn_hash = initial["placement"]["allocated_node"]
    assert initial_crn_hash, "Placement should expose a CRN node hash"

    _wait_for_ssh(aleph_cli, instance_hash, private_key_path, timeout=60)

    # Persist a marker we'll verify after migration. `sync` flushes the page
    # cache before the VM is stopped by the unlink so the marker hits disk.
    marker = uuid.uuid4().hex
    _vm_ssh_run(
        aleph_cli, instance_hash, private_key_path,
        f"echo {marker} > /root/migration-marker.txt && sync",
    )

    # --- Phase 2: Unlink the initial CRN and verify migration ---

    aleph_cli("node", "unlink", "--crn", initial_crn_hash, "--chain", "eth")

    migrated = _wait_for_dispatched(
        aleph_cli, instance_hash, timeout=300, different_from=initial_crn_hash,
    )
    new_crn_hash = migrated["placement"]["allocated_node"]
    assert new_crn_hash != initial_crn_hash, (
        f"Scheduler should migrate to a different CRN, but got {new_crn_hash}"
    )

    _wait_for_ssh(aleph_cli, instance_hash, private_key_path, timeout=60)

    # Verify disk state was preserved: the marker written on CRN A must still be there.
    persisted = _vm_ssh_run(
        aleph_cli, instance_hash, private_key_path,
        "cat /root/migration-marker.txt",
    ).strip()
    assert persisted == marker, (
        f"Migration should preserve disk state — expected marker {marker!r} "
        f"but got {persisted!r}"
    )
