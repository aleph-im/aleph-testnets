"""Shared helpers for instance/VM integration tests.

SSH reaches the VM over the CRN's IPv4 + the host-side mapped port reported by
`instance show --verbose` (`mapped_ports`). The CLI's own `instance ssh` uses
the VM's IPv6, which is not reachable in this CI.
"""
import subprocess
import time
from dataclasses import dataclass
from urllib.parse import urlparse

import pytest

# Nodestatus account: owns the testnet's corechannel aggregate (registered CRNs).
NODESTATUS_ADDR = "0x70997970C51812dc3A010C7d01b50e0d17dc79C8"


def poll(description, fetch, timeout, interval=5):
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


def resolve_crn_address(aleph_cli, crn_hash: str) -> str:
    """Return the CRN's corechannel-registered API URL (e.g. http://1.2.3.4:4020)."""
    nodes = aleph_cli(
        "node", "list", "--type", "crn", "--all",
        "--corechannel-address", NODESTATUS_ADDR,
        parse_json=True,
    )
    for n in nodes or []:
        if n.get("hash") == crn_hash:
            return n["address"].rstrip("/")
    pytest.fail(f"CRN {crn_hash} not found in corechannel aggregate")


def resolve_crn_host(aleph_cli, crn_hash: str) -> str:
    """Return the CRN's reachable hostname (from its corechannel-registered URL)."""
    return urlparse(resolve_crn_address(aleph_cli, crn_hash)).hostname


def _ssh_base(private_key_path, host, port):
    return [
        "ssh",
        "-i", private_key_path,
        "-p", str(port),
        "-o", "StrictHostKeyChecking=no",
        "-o", "UserKnownHostsFile=/dev/null",
        "-o", "ConnectTimeout=5",
        f"root@{host}",
    ]


def ssh_run(private_key_path, host, port, command, timeout=15):
    """Run a one-shot command over SSH; return stdout. Raises on non-zero exit."""
    result = subprocess.run(
        _ssh_base(private_key_path, host, port) + [command],
        capture_output=True, text=True, timeout=timeout, check=True,
    )
    return result.stdout


def ssh_ok(private_key_path, host, port, timeout=15) -> bool:
    """Return True if a one-shot `echo hello` SSH round-trip succeeds."""
    try:
        result = subprocess.run(
            _ssh_base(private_key_path, host, port) + ["echo hello"],
            capture_output=True, text=True, timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return False
    return result.returncode == 0 and "hello" in result.stdout


def wait_for_ssh(private_key_path, host, port, timeout=60):
    """Poll SSH until `echo hello` succeeds on host:port."""
    return poll(
        f"SSH into {host}:{port}",
        lambda: True if ssh_ok(private_key_path, host, port) else None,
        timeout=timeout,
    )


def wait_for_dispatched(aleph_cli, vm_hash, timeout=300, *, different_from=None, required_port="22"):
    """Poll `instance show --verbose` until the VM is dispatched with the named
    host-side mapped port present. If `different_from` is set, also require the
    allocated node hash to differ (used to detect post-unlink migration)."""
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
        if mapped.get(required_port) is None:
            return None
        return data
    label = f"VM {vm_hash[:12]} dispatched + port {required_port}"
    if different_from is not None:
        label += f" (different from {different_from[:12]})"
    return poll(label, fetch, timeout=timeout)


@dataclass
class DispatchedVM:
    """A dispatched VM's reachable coordinates. Mutable: `refresh` re-reads
    placement because the mapped SSH port can change after start / restore."""
    hash: str
    crn_hash: str
    crn_host: str
    ssh_port: int

    def refresh(self, aleph_cli, timeout=300) -> "DispatchedVM":
        data = wait_for_dispatched(aleph_cli, self.hash, timeout=timeout)
        self.crn_hash = data["placement"]["allocated_node"]
        self.crn_host = resolve_crn_host(aleph_cli, self.crn_hash)
        self.ssh_port = int(data["mapped_ports"]["22"])
        return self


def delete_instance(aleph_cli, vm_hash):
    """Best-effort FORGET of an instance so the scheduler de-allocates it and the
    CRN frees the VM's resources. Meant for test/fixture teardown: it never fails
    the test (`check=False`).

    Cleaning up is not optional hygiene here — leaked VMs occupy CRN capacity, and
    the migration test needs a free CRN to migrate onto. Every instance-creating
    test must call this so capacity can't creep up as the suite grows.

    -y is required: without it the CLI prompts for confirmation, and on the CI's
    non-TTY stdin that reads EOF and silently aborts the FORGET.
    """
    aleph_cli("instance", "delete", vm_hash, "-y", "--chain", "eth", check=False)


def create_dispatched_instance(aleph_cli, rootfs_hash, public_key_path, name, *,
                               vcpus="1", memory="2GiB", disk_size="4GiB", timeout=300) -> DispatchedVM:
    """Create an instance, wait for the scheduler to dispatch it, and resolve
    its reachable CRN host + mapped SSH port. Does not wait for SSH."""
    result = aleph_cli(
        "instance", "create", name,
        "--image", rootfs_hash,
        "--vcpus", vcpus,
        "--memory", memory,
        "--disk-size", disk_size,
        "--ssh-pubkey-file", public_key_path,
        "--chain", "eth",
        parse_json=True,
    )
    vm_hash = result["item_hash"]
    assert vm_hash, "Instance create should return an item_hash"
    data = wait_for_dispatched(aleph_cli, vm_hash, timeout=timeout)
    crn_hash = data["placement"]["allocated_node"]
    return DispatchedVM(
        hash=vm_hash,
        crn_hash=crn_hash,
        crn_host=resolve_crn_host(aleph_cli, crn_hash),
        ssh_port=int(data["mapped_ports"]["22"]),
    )
