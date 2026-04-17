import json
import subprocess
import time
import urllib.request
import urllib.error
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


@pytest.mark.timeout(420)
def test_instance_create_and_ssh(
    aleph_cli, rootfs_image, ssh_key_pair, scheduler_api_url, ccn_url
):
    """End-to-end: create instance → scheduler dispatches → CRN boots → SSH in."""
    private_key_path, public_key_path = ssh_key_pair

    # Step 1: Upload rootfs to CCN
    upload_result = aleph_cli(
        "file", "upload", rootfs_image, "--storage-engine", "storage", parse_json=True
    )
    rootfs_hash = upload_result["item_hash"]
    assert rootfs_hash, "Upload should return an item_hash"

    # Step 2: Create instance
    instance_result = aleph_cli(
        "instance", "create",
        "--image", rootfs_hash,
        "--vcpus", "1",
        "--memory", "2GiB",
        "--disk-size", "4GiB",
        "--ssh-pubkey-file", public_key_path,
        parse_json=True,
    )
    instance_hash = instance_result["item_hash"]
    assert instance_hash, "Instance create should return an item_hash"

    # Step 3: Poll scheduler-api for allocation (discover CRN)
    def fetch_allocation():
        data = _http_get_json(
            f"{scheduler_api_url}/api/v0/allocation/{instance_hash}"
        )
        if data and data.get("node", {}).get("url"):
            return data
        return None

    allocation = _poll("Scheduler allocation", fetch_allocation, timeout=180)
    crn_url_raw = allocation["node"]["url"]  # e.g. "http://1.2.3.4:4020"
    assert crn_url_raw, "Allocation should include a CRN URL"

    # Parse CRN IP from the URL
    parsed = urlparse(crn_url_raw)
    crn_host = parsed.hostname
    crn_port = parsed.port or 4020
    crn_base = f"http://{crn_host}:{crn_port}"

    # Step 4: Poll CRN for running VM with mapped SSH port
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

    ssh_host_port = _poll("CRN VM boot + SSH port", fetch_ssh_port, timeout=120)

    # Step 5: SSH into the VM
    def try_ssh():
        result = subprocess.run(
            [
                "ssh",
                "-i", private_key_path,
                "-p", str(ssh_host_port),
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
