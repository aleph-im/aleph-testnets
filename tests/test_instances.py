import subprocess
import time
from urllib.parse import urlparse

import pytest

# Nodestatus account: owns the testnet's corechannel aggregate (registered CRNs).
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
    """Return the CRN's reachable hostname (from its registered URL in the
    corechannel aggregate), given the node hash from `instance show` placement.
    """
    nodes = aleph_cli(
        "node", "list", "--type", "crn", "--all",
        "--corechannel-address", NODESTATUS_ADDR,
        parse_json=True,
    )
    for n in nodes or []:
        if n.get("hash") == crn_hash:
            return urlparse(n["address"]).hostname
    pytest.fail(f"CRN {crn_hash} not found in corechannel aggregate")


@pytest.mark.timeout(420)
def test_instance_create_and_ssh(aleph_cli, rootfs_image, ssh_key_pair):
    """End-to-end: create instance → scheduler dispatches → CRN boots → SSH in.

    Discovery uses `aleph instance show --verbose` (scheduler placement +
    CRN-reported mapped ports in one call). SSH goes over the CRN's IPv4 plus
    the host-side mapped port — the CLI's own `instance ssh` connects via the
    VM's IPv6 which isn't reachable in this CI.
    """
    private_key_path, public_key_path = ssh_key_pair

    upload_result = aleph_cli(
        "file", "upload", rootfs_image, "--storage-engine", "storage", "--chain", "eth", parse_json=True
    )
    rootfs_hash = upload_result["item_hash"]
    assert rootfs_hash, "Upload should return an item_hash"

    instance_result = aleph_cli(
        "instance", "create",
        "test-instance",
        "--image", rootfs_hash,
        "--vcpus", "1",
        "--memory", "2GiB",
        "--disk-size", "4GiB",
        "--ssh-pubkey-file", public_key_path,
        "--chain", "eth",
        parse_json=True,
    )
    instance_hash = instance_result["item_hash"]
    assert instance_hash, "Instance create should return an item_hash"

    # Wait for scheduler dispatch + CRN boot + SSH port mapping.
    def vm_ready():
        data = aleph_cli(
            "instance", "show", instance_hash, "--verbose",
            parse_json=True, check=False,
        )
        if not isinstance(data, dict):
            return None
        node = data.get("placement", {}).get("allocated_node")
        mapped = data.get("mapped_ports") or {}
        if node and mapped.get("22") is not None:
            return data
        return None

    show = _poll("Instance dispatched + SSH port mapped", vm_ready, timeout=300)
    crn_host = _resolve_crn_host(aleph_cli, show["placement"]["allocated_node"])
    ssh_port = int(show["mapped_ports"]["22"])

    def try_ssh():
        result = subprocess.run(
            [
                "ssh",
                "-i", private_key_path,
                "-p", str(ssh_port),
                "-o", "StrictHostKeyChecking=no",
                "-o", "UserKnownHostsFile=/dev/null",
                "-o", "ConnectTimeout=5",
                f"root@{crn_host}",
                "echo hello",
            ],
            capture_output=True,
            text=True,
            timeout=15,
        )
        if result.returncode == 0 and "hello" in result.stdout:
            return result.stdout.strip()
        return None

    output = _poll("SSH into instance", try_ssh, timeout=60)
    assert "hello" in output
