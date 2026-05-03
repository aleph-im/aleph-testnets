"""Integration test for VM migration between CRNs.

Flow: create instance → SSH on initial CRN + write a marker file → unlink that CRN →
verify scheduler reallocates → SSH on new CRN + verify the marker survived (disk
state preserved across migration).

Requires:
- Two CRNs provisioned and linked (CRN_COUNT=2)
- Scheduler-rs with dispatch enabled
- Ubuntu rootfs image (ALEPH_TESTNET_ROOTFS)
"""
import json
import subprocess
import time
import urllib.request
import urllib.error
import uuid
from urllib.parse import urlparse

import pytest


def _poll(description, fetch, timeout, interval=5):
    """Poll fetch() until it returns a truthy value or timeout is reached.

    fetch() should return the result on success or None to keep polling.
    It may raise to keep polling (exceptions are swallowed until timeout).
    """
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


def _http_get_json(url):
    """GET a URL and return parsed JSON, or None on HTTP error."""
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    try:
        resp = urllib.request.urlopen(req, timeout=10)
        return json.loads(resp.read())
    except (urllib.error.HTTPError, urllib.error.URLError, OSError):
        return None


def _crn_base_url(raw_url) -> tuple[str, str]:
    """Normalize a CRN URL to (base_url, hostname)."""
    parsed = urlparse(raw_url)
    host = parsed.hostname
    port = parsed.port or 4020
    return f"http://{host}:{port}", host


def _find_crn_hash(crn_nodes, allocation_url):
    """Match a scheduler allocation URL to a CRN node hash."""
    alloc_host = urlparse(allocation_url).hostname
    for node in crn_nodes:
        if urlparse(node["address"]).hostname == alloc_host:
            return node["hash"]
    pytest.fail(
        f"No CRN in corechannel aggregate matches allocation URL {allocation_url}"
    )


def _ssh_run(private_key_path, host, port, command, timeout=15):
    """Run a one-shot SSH command and return stdout. Raises on non-zero exit."""
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
        capture_output=True,
        text=True,
        timeout=timeout,
        check=True,
    )
    return result.stdout


def _wait_for_ssh(private_key_path, host, port, timeout=60):
    """Poll SSH until 'echo hello' succeeds on the given host:port."""
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
            capture_output=True,
            text=True,
            timeout=15,
        )
        if result.returncode == 0 and "hello" in result.stdout:
            return result.stdout.strip()
        return None

    return _poll(f"SSH into {host}:{port}", try_ssh, timeout=timeout)


def _wait_for_vm_ssh_port(crn_base, instance_hash, timeout=120):
    """Poll a CRN's execution list until the VM is running with a mapped SSH port."""
    def fetch_ssh_port():
        data = _http_get_json(f"{crn_base}/v2/about/executions/list")
        if not data:
            return None
        execution = data.get(instance_hash)
        if not execution:
            return None
        if not execution.get("running"):
            return None
        networking = execution.get("networking", {})
        mapped_ports = networking.get("mapped_ports", {})
        port_22 = mapped_ports.get("22") or mapped_ports.get(22)
        if port_22 and port_22.get("host"):
            return int(port_22["host"])
        return None

    return _poll(f"VM boot + SSH port on {crn_base}", fetch_ssh_port, timeout=timeout)


@pytest.mark.timeout(900)
def test_instance_migration(
    aleph_cli, rootfs_image, ssh_key_pair, scheduler_api_url, crn_nodes
):
    """End-to-end: create instance → SSH → unlink CRN → scheduler migrates → SSH on new CRN."""
    private_key_path, public_key_path = ssh_key_pair

    # --- Phase 1: Create instance and verify on initial CRN ---

    # Upload rootfs
    upload_result = aleph_cli(
        "file", "upload", rootfs_image, "--storage-engine", "storage", parse_json=True
    )
    rootfs_hash = upload_result["item_hash"]
    assert rootfs_hash, "Upload should return an item_hash"

    # Create instance
    instance_result = aleph_cli(
        "instance", "create",
        "--image", rootfs_hash,
        "--disk-size", "4GiB",
        "--ssh-pubkey-file", public_key_path,
        "--vcpus", "1",
        "--memory", "2GiB",
        parse_json=True,
    )
    instance_hash = instance_result["item_hash"]
    assert instance_hash, "Instance create should return an item_hash"

    # Poll scheduler-api for initial allocation
    def fetch_allocation():
        data = _http_get_json(
            f"{scheduler_api_url}/api/v0/allocation/{instance_hash}"
        )
        if data and data.get("node", {}).get("url"):
            return data
        return None

    allocation = _poll("Scheduler allocation", fetch_allocation, timeout=180)
    initial_crn_url = allocation["node"]["url"]
    assert initial_crn_url, "Allocation should include a CRN URL"

    initial_crn_base, initial_crn_host = _crn_base_url(initial_crn_url)

    # Wait for VM to boot on initial CRN
    ssh_port = _wait_for_vm_ssh_port(initial_crn_base, instance_hash, timeout=120)

    # SSH baseline check
    output = _wait_for_ssh(private_key_path, initial_crn_host, ssh_port, timeout=60)
    assert "hello" in output

    # Write a unique marker so we can verify disk state survives migration.
    # `sync` flushes the page cache before the VM is stopped by the unlink.
    marker = uuid.uuid4().hex
    _ssh_run(
        private_key_path, initial_crn_host, ssh_port,
        f"echo {marker} > /root/migration-marker.txt && sync",
    )

    # --- Phase 2: Unlink the initial CRN and verify migration ---

    # Find the CRN's node hash
    crn_hash = _find_crn_hash(crn_nodes, initial_crn_url)

    # Unlink the CRN from the CCN
    aleph_cli("node", "unlink", "--crn", crn_hash)

    # Poll scheduler-api until the allocation moves to a different CRN
    def fetch_new_allocation():
        data = _http_get_json(
            f"{scheduler_api_url}/api/v0/allocation/{instance_hash}"
        )
        if not data or not data.get("node", {}).get("url"):
            return None
        new_url = data["node"]["url"]
        # Must be a *different* CRN than the initial one
        new_host = urlparse(new_url).hostname
        if new_host != initial_crn_host:
            return data
        return None

    new_allocation = _poll(
        "Scheduler reallocation to new CRN", fetch_new_allocation, timeout=300
    )
    new_crn_url = new_allocation["node"]["url"]
    new_crn_base, new_crn_host = _crn_base_url(new_crn_url)

    assert new_crn_host != initial_crn_host, (
        f"Scheduler should migrate to a different CRN, "
        f"but got the same host: {new_crn_host}"
    )

    # Wait for VM to boot on the new CRN
    new_ssh_port = _wait_for_vm_ssh_port(new_crn_base, instance_hash, timeout=180)

    # SSH into the migrated VM
    output = _wait_for_ssh(private_key_path, new_crn_host, new_ssh_port, timeout=60)
    assert "hello" in output

    # Verify disk state was preserved: the marker written on CRN A must still be there.
    persisted = _ssh_run(
        private_key_path, new_crn_host, new_ssh_port,
        "cat /root/migration-marker.txt",
    ).strip()
    assert persisted == marker, (
        f"Migration should preserve disk state — expected marker {marker!r} "
        f"but got {persisted!r}"
    )
