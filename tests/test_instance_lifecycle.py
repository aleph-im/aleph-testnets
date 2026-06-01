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
    poll,
    ssh_ok,
    ssh_run,
    wait_for_ssh,
)


@pytest.fixture(scope="module")
def running_vm(aleph_cli, rootfs_hash, ssh_key_pair):
    """One dispatched, SSH-reachable VM shared by this module's tests."""
    private_key_path, public_key_path = ssh_key_pair
    vm = create_dispatched_instance(
        aleph_cli, rootfs_hash, public_key_path, "lifecycle-instance",
    )
    wait_for_ssh(private_key_path, vm.crn_host, vm.ssh_port, timeout=120)
    return vm


@pytest.mark.timeout(420)
def test_instance_logs(running_vm, aleph_cli):
    """`aleph instance logs` returns console output from the running VM.

    `logs` streams and does not exit on its own, so we cap it with a timeout
    (the aleph_cli fixture returns the partial capture on TimeoutExpired) and
    assert we received non-empty console output.
    """
    result = aleph_cli("instance", "logs", running_vm.hash, timeout=25)
    assert result.stdout.strip(), (
        f"Expected non-empty logs from the VM. stderr: {result.stderr!r}"
    )
