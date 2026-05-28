"""Integration test for VM migration between CRNs.

Flow: create instance → SSH on initial CRN + write a marker file → unlink that
CRN → wait for scheduler to reallocate → SSH on new CRN + verify the marker
survived (disk state preserved across migration).

Discovery uses `aleph instance show --verbose` (scheduler placement + CRN-
reported mapped ports). SSH goes over the CRN's IPv4 + the host-side mapped
port (the CLI's `instance ssh` uses the VM's IPv6, which is not reachable from
this CI). Node operations go through `aleph node unlink` / `aleph node list`.

Requires:
- Two CRNs provisioned and linked (CRN_COUNT=2)
- Scheduler-rs with dispatch enabled
- Ubuntu rootfs image (ALEPH_TESTNET_ROOTFS)
"""
import subprocess
import time
import uuid
from urllib.parse import urlparse

import pytest

# Nodestatus account owning the testnet's corechannel aggregate.
NODESTATUS_ADDR = "0x70997970C51812dc3A010C7d01b50e0d17dc79C8"


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


def _resolve_crn_host(aleph_cli, crn_hash: str) -> str:
    """Return the CRN's reachable hostname (from its corechannel-registered URL)."""
    nodes = aleph_cli(
        "node", "list", "--type", "crn", "--all",
        "--corechannel-address", NODESTATUS_ADDR,
        parse_json=True,
    )
    for n in nodes or []:
        if n.get("hash") == crn_hash:
            return urlparse(n["address"]).hostname
    pytest.fail(f"CRN {crn_hash} not found in corechannel aggregate")


def _ssh_run(private_key_path, host, port, command, timeout=15):
    """Run a one-shot command over SSH; return stdout. Raises on non-zero exit."""
    result = subprocess.run(
        [
            "ssh",
            "-i", private_key_path,
            "-p", str(port),
            "-o", "StrictHostKeyChecking=no",
            "-o", "UserKnownHostsFile=/dev/null",
            "-o", "ConnectTimeout=5",
            f"root@{host}",
            command,
        ],
        capture_output=True, text=True, timeout=timeout, check=True,
    )
    return result.stdout


def _wait_for_ssh(private_key_path, host, port, timeout=60):
    """Poll SSH until `echo hello` succeeds on host:port."""
    def try_ssh():
        result = subprocess.run(
            [
                "ssh",
                "-i", private_key_path,
                "-p", str(port),
                "-o", "StrictHostKeyChecking=no",
                "-o", "UserKnownHostsFile=/dev/null",
                "-o", "ConnectTimeout=5",
                f"root@{host}",
                "echo hello",
            ],
            capture_output=True, text=True, timeout=15,
        )
        if result.returncode == 0 and "hello" in result.stdout:
            return result.stdout.strip()
        return None
    return _poll(f"SSH into {host}:{port}", try_ssh, timeout=timeout)


def _wait_for_dispatched(aleph_cli, vm_hash, timeout=300, *, different_from=None):
    """Poll `instance show --verbose` until the VM is dispatched with a mapped
    SSH port. If `different_from` is set, also require the allocated node hash
    to differ from that value (used to detect post-unlink migration).
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
    """End-to-end: create → SSH → unlink CRN → scheduler migrates → SSH new CRN."""
    private_key_path, public_key_path = ssh_key_pair

    # --- Phase 1: Create instance, verify on initial CRN ---

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
    initial_host = _resolve_crn_host(aleph_cli, initial_crn_hash)
    initial_port = int(initial["mapped_ports"]["22"])

    _wait_for_ssh(private_key_path, initial_host, initial_port, timeout=60)

    # Persist a marker we'll verify after migration. `sync` flushes the page
    # cache before the VM is stopped by the unlink so the marker hits disk.
    marker = uuid.uuid4().hex
    _ssh_run(
        private_key_path, initial_host, initial_port,
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
    new_host = _resolve_crn_host(aleph_cli, new_crn_hash)
    new_port = int(migrated["mapped_ports"]["22"])

    _wait_for_ssh(private_key_path, new_host, new_port, timeout=60)

    persisted = _ssh_run(
        private_key_path, new_host, new_port,
        "cat /root/migration-marker.txt",
    ).strip()
    assert persisted == marker, (
        f"Migration should preserve disk state — expected marker {marker!r} "
        f"but got {persisted!r}"
    )
