import pytest

from tests.vm_helpers import create_dispatched_instance, delete_instance, wait_for_ssh


@pytest.mark.timeout(420)
def test_instance_create_and_ssh(aleph_cli, rootfs_hash, ssh_key_pair):
    """End-to-end: create instance → scheduler dispatches → CRN boots → SSH in.

    Discovery + SSH transport are handled by the shared helpers (CRN IPv4 +
    host-side mapped SSH port from `instance show --verbose`). The VM is deleted
    afterwards so it doesn't hold CRN capacity for later tests (e.g. migration).
    """
    private_key_path, public_key_path = ssh_key_pair
    vm = create_dispatched_instance(
        aleph_cli, rootfs_hash, public_key_path, "test-instance",
    )
    try:
        # Fails the test via poll() if the `echo hello` round-trip (checked
        # inside ssh_ok) never succeeds — no separate assert needed.
        wait_for_ssh(private_key_path, vm.crn_host, vm.ssh_port, timeout=60)
    finally:
        delete_instance(aleph_cli, vm.hash)
