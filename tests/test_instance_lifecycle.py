"""Lifecycle integration tests for a dispatched VM (non-destructive ops share
one module-scoped VM; each test leaves it running). Ordered least → most
disruptive: logs → port-forward → reboot → stop/start.
"""
import json
import urllib.request
import uuid

import pytest

from tests.vm_helpers import (
    create_dispatched_instance,
    delete_instance,
    poll,
    ssh_ok,
    ssh_run,
    wait_for_ssh,
)


@pytest.fixture(scope="module")
def running_vm(aleph_cli, rootfs_hash, ssh_key_pair):
    """One dispatched, SSH-reachable VM shared by this module's tests.

    Deleted on teardown so it doesn't hold CRN capacity for the rest of the run
    (the migration test needs a free CRN to migrate onto).
    """
    private_key_path, public_key_path = ssh_key_pair
    vm = create_dispatched_instance(
        aleph_cli, rootfs_hash, public_key_path, "lifecycle-instance",
    )
    wait_for_ssh(private_key_path, vm.crn_host, vm.ssh_port, timeout=120)
    yield vm
    delete_instance(aleph_cli, vm.hash)


@pytest.mark.timeout(420)
def test_instance_logs(running_vm, aleph_cli):
    """`aleph instance logs` returns console output from the running VM.

    `logs` streams and does not exit on its own, so we cap it with a timeout
    (the aleph_cli fixture returns the partial capture on TimeoutExpired) and
    assert we received non-empty console output.
    """
    result = aleph_cli("instance", "logs", running_vm.hash, "--chain", "eth", timeout=25)
    assert result.stdout.strip(), (
        f"Expected non-empty logs from the VM. stderr: {result.stderr!r}"
    )


def _forward_present(forwards, port) -> bool:
    """Tolerant check that `port` appears in `port-forward list` JSON, whose
    exact shape may vary. Confirm/tighten against live output."""
    return str(port) in json.dumps(forwards)


def _wait_forward_host_port(aleph_cli, vm_hash, vm_port, timeout=120) -> int:
    """Poll `instance show --verbose` for the CRN host-side port mapped to
    `vm_port`."""
    def fetch():
        data = aleph_cli("instance", "show", vm_hash, "--verbose",
                         parse_json=True, check=False)
        if isinstance(data, dict):
            mapped = (data.get("mapped_ports") or {}).get(str(vm_port))
            if mapped is not None:
                return int(mapped)
        return None
    return poll(f"host-side mapped port for VM port {vm_port}", fetch, timeout=timeout)


@pytest.mark.timeout(360)
def test_instance_port_forward(running_vm, aleph_cli, ssh_key_pair):
    """Create an IPv4 TCP port forward and verify real connectivity through it."""
    private_key_path, _ = ssh_key_pair
    vm_port = 8000
    token = uuid.uuid4().hex

    aleph_cli("instance", "port-forward", "create", running_vm.hash, str(vm_port),
              "--tcp", "true", "--chain", "eth")
    try:
        aleph_cli("instance", "port-forward", "refresh", running_vm.hash, "--chain", "eth")

        forwards = aleph_cli("instance", "port-forward", "list",
                             "--vm-id", running_vm.hash, parse_json=True)
        assert _forward_present(forwards, vm_port), (
            f"Forward for port {vm_port} not found in: {forwards}"
        )

        # Serve a known token over the forwarded port from inside the VM.
        ssh_run(
            private_key_path, running_vm.crn_host, running_vm.ssh_port,
            f"echo {token} > /root/pf.txt && "
            f"(setsid python3 -m http.server {vm_port} --directory /root "
            f">/tmp/pf.log 2>&1 &) && sleep 1",
        )

        host_port = _wait_forward_host_port(aleph_cli, running_vm.hash, vm_port)

        def fetch_token():
            try:
                url = f"http://{running_vm.crn_host}:{host_port}/pf.txt"
                with urllib.request.urlopen(url, timeout=5) as resp:
                    return resp.read().decode()
            except Exception:
                return None

        body = poll("HTTP through port forward", fetch_token, timeout=60)
        assert token in body, f"Expected token {token!r} through forward, got {body!r}"
    finally:
        aleph_cli("instance", "port-forward", "delete", running_vm.hash,
                  "--port", str(vm_port), "--chain", "eth", check=False)
        try:
            ssh_run(private_key_path, running_vm.crn_host, running_vm.ssh_port,
                    "pkill -f http.server || true")
        except Exception:
            pass


@pytest.mark.timeout(300)
def test_instance_reboot(running_vm, aleph_cli, ssh_key_pair):
    """`aleph instance reboot` restarts the VM; the kernel boot_id changes and
    the VM becomes SSH-reachable again."""
    private_key_path, _ = ssh_key_pair
    before = ssh_run(
        private_key_path, running_vm.crn_host, running_vm.ssh_port,
        "cat /proc/sys/kernel/random/boot_id",
    ).strip()
    assert before, "Should read a boot_id before reboot"

    aleph_cli("instance", "reboot", running_vm.hash, "--chain", "eth")

    def new_boot_id():
        try:
            bid = ssh_run(
                private_key_path, running_vm.crn_host, running_vm.ssh_port,
                "cat /proc/sys/kernel/random/boot_id",
            ).strip()
        except Exception:
            return None
        return bid if bid and bid != before else None

    after = poll("VM reboot (boot_id change)", new_boot_id, timeout=180)
    assert after != before, "boot_id should change after a real reboot"


@pytest.mark.timeout(420)
def test_instance_stop_start(running_vm, aleph_cli, ssh_key_pair):
    """`aleph instance stop` brings the VM down; `instance start` brings it back."""
    private_key_path, _ = ssh_key_pair
    wait_for_ssh(private_key_path, running_vm.crn_host, running_vm.ssh_port, timeout=60)

    aleph_cli("instance", "stop", running_vm.hash, "--chain", "eth")
    poll(
        "VM stopped (SSH unreachable)",
        lambda: True if not ssh_ok(private_key_path, running_vm.crn_host, running_vm.ssh_port) else None,
        timeout=120,
    )

    aleph_cli("instance", "start", running_vm.hash, "--chain", "eth")
    running_vm.refresh(aleph_cli, timeout=300)  # mapped SSH port may change
    output = wait_for_ssh(private_key_path, running_vm.crn_host, running_vm.ssh_port, timeout=120)
    assert output, "VM should be SSH-reachable again after start"
