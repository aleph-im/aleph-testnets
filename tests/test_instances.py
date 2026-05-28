import time

import pytest


def _poll(description, fetch, timeout, interval=5):
    """Poll fetch() until it returns a truthy value or timeout is reached.

    fetch() should return the result on success or None to keep polling.
    Exceptions are swallowed until the deadline (the last one is reported on
    failure).
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


@pytest.mark.timeout(420)
def test_instance_create_and_ssh(aleph_cli, rootfs_image, ssh_key_pair):
    """End-to-end: create instance → scheduler dispatches → CRN boots → SSH in.

    Discovery (scheduler placement + CRN networking + mapped SSH port) is
    delegated to `aleph instance show --verbose`. The SSH step uses
    `aleph instance ssh` (which connects over the VM's IPv6).
    """
    private_key_path, public_key_path = ssh_key_pair

    # Step 1: Upload rootfs to CCN
    upload_result = aleph_cli(
        "file", "upload", rootfs_image, "--storage-engine", "storage", "--chain", "eth", parse_json=True
    )
    rootfs_hash = upload_result["item_hash"]
    assert rootfs_hash, "Upload should return an item_hash"

    # Step 2: Create instance
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

    # Step 3: Wait for scheduler dispatch + CRN boot + SSH port mapping.
    # `instance show --verbose` rolls up the CCN INSTANCE message, scheduler
    # placement, and live CRN networking in one call. The presence of a host
    # port for VM port 22 means the VM is running with SSH reachable.
    def vm_has_ssh_port():
        data = aleph_cli(
            "instance", "show", instance_hash, "--verbose",
            parse_json=True, check=False,
        )
        if not isinstance(data, dict):
            return None
        mapped = data.get("mapped_ports") or {}
        # `mapped_ports` is keyed by VM port (string in JSON).
        if mapped.get("22") is not None:
            return data
        return None

    _poll("Instance dispatched + SSH port mapped", vm_has_ssh_port, timeout=300)

    # Step 4: SSH in via the CLI. Polls because sshd inside the VM may need a
    # moment after the port shows up to accept connections.
    def try_ssh():
        result = aleph_cli(
            "instance", "ssh", instance_hash,
            "--identity", private_key_path,
            "--", "echo", "hello",
            check=False,
        )
        if result.returncode == 0 and "hello" in result.stdout:
            return result.stdout.strip()
        return None

    output = _poll("SSH into instance", try_ssh, timeout=60)
    assert "hello" in output
