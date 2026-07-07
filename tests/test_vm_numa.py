"""NUMA placement check (Phase 3 increment C1).

The Rust supervisor auto-places persistent QEMU VMs on NUMA nodes (pack-first)
and pins their vCPUs with a systemd `AllowedCPUs=` drop-in written at
  /etc/systemd/system/aleph-vm-controller@<hash>.service.d/numa.conf
By design the whole mechanism is INERT on a single-NUMA-node host: every
CONFIG_NUMA kernel exposes node0 even on one socket, and pinning to the only
node is all host CPUs (a no-op), so no drop-in is written and VmInfo.numa_node
stays None. The topology is still reported in HostInfo.numa_nodes (pure
reporting) but nothing is pinned.

The DigitalOcean CRN droplet (s-8vcpu-16gb) is almost certainly single-node, so
this test asserts that INERT contract: an instance created on it gets NO numa.conf
drop-in and (where readable) numa_node None. That is the honest assertion this
runner can make.

A true pack-first assertion (drop-in present, AllowedCPUs set, numa_node pinned)
needs a MULTI-SOCKET runner AND the Rust supervisor active; the test does assert
that automatically if it ever lands on such a host, but flag it: the default DO
runner will only ever exercise the inert path. See ASSUMPTIONS.

Robust to a normal run: this creates a plain instance through the CLI/CCN path
and inspects the host over root SSH; it never requires the SNP server and skips
nothing in the common case (the inert assertion holds under either supervisor
impl, since single-node placement is inert everywhere).
"""
import os
import subprocess

import pytest

from tests.vm_helpers import create_dispatched_instance, delete_instance, wait_for_ssh

SUPERVISOR_ENV_FILE = "/etc/aleph-vm/supervisor.env"
DEFAULT_SUPERVISOR_SOCKET = "/var/lib/aleph/vm/supervisor.sock"

# See tests/test_vm_snp.py for why msgpack is stubbed (candidate deb omits it).
_MSGPACK_STUB = (
    "import sys, types\n"
    "try:\n"
    "    import msgpack  # noqa: F401\n"
    "except ModuleNotFoundError:\n"
    "    sys.modules['msgpack'] = types.ModuleType('msgpack')\n"
)

# Reads VmInfo.numa_node over the supervisor gRPC socket. numa_node is the
# supervisor's source of truth; the agent's HTTP API reports it as None
# unconditionally (translate.py), so gRPC GetVm is the only way to read the real
# value. Prints NUMA_NODE=<int|none>.
_GETVM_SNIPPET = _MSGPACK_STUB + '''
import asyncio, os
from aleph.vm.supervisor_interface.client import GrpcSupervisor
from aleph.vm.supervisor_interface.types import VmId

async def main():
    sup = GrpcSupervisor(os.environ["SUPERVISOR_SOCKET"])
    try:
        info = await sup.get_vm(VmId(os.environ["VM_ID"]))
        node = info.numa_node
        print(f"NUMA_NODE={'none' if node is None else node}")
    finally:
        await sup.close()

asyncio.run(main())
'''


def _host_ssh(key, host, command, *, timeout=60, stdin=None):
    return subprocess.run(
        [
            "ssh", "-i", key,
            "-o", "StrictHostKeyChecking=no",
            "-o", "UserKnownHostsFile=/dev/null",
            "-o", "ConnectTimeout=10",
            f"root@{host}",
            command,
        ],
        input=stdin, capture_output=True, text=True, timeout=timeout,
    )


def _numa_node_count(key, host) -> int:
    r = _host_ssh(
        key, host,
        "ls -d /sys/devices/system/node/node[0-9]* 2>/dev/null | wc -l",
        timeout=30,
    )
    try:
        return int(r.stdout.strip())
    except ValueError:
        # No sysfs NUMA at all (CONFIG_NUMA off) behaves like a single node.
        return 0


def _supervisor_impl(key, host) -> str:
    """'rust' or 'python' (or 'unknown'), from the running MainPID's exe."""
    pid = _host_ssh(
        key, host,
        "systemctl show -p MainPID --value aleph-vm-supervisor.service",
        timeout=30,
    ).stdout.strip()
    if not pid or pid == "0":
        return "unknown"
    exe = _host_ssh(key, host, f"readlink -f /proc/{pid}/exe", timeout=30).stdout.strip()
    return "python" if "python" in os.path.basename(exe) else "rust"


def _dropin_present(key, host, vm_hash) -> bool:
    path = f"/etc/systemd/system/aleph-vm-controller@{vm_hash}.service.d/numa.conf"
    r = _host_ssh(key, host, f"test -f {path} && echo yes || echo no", timeout=30)
    return r.stdout.strip() == "yes"


def _dropin_body(key, host, vm_hash) -> str:
    path = f"/etc/systemd/system/aleph-vm-controller@{vm_hash}.service.d/numa.conf"
    return _host_ssh(key, host, f"cat {path} 2>/dev/null || true", timeout=30).stdout


def _discover_socket(key, host) -> str:
    r = _host_ssh(
        key, host,
        f"grep -E '^ALEPH_VM_SUPERVISOR_GRPC_SOCKET=' {SUPERVISOR_ENV_FILE} "
        "| tail -1 | cut -d= -f2- || true",
        timeout=30,
    )
    return (r.stdout or "").strip() or DEFAULT_SUPERVISOR_SOCKET


def _read_numa_node(key, host, vm_hash, socket_path):
    """Best-effort VmInfo.numa_node via on-host gRPC GetVm.

    Returns 'none', an int-as-str, or None if unreadable (e.g. the CRN still
    runs a baseline deb that predates the gRPC supervisor_interface). Never
    fails the test on its own.
    """
    _host_ssh(key, host, "cat > /tmp/numa_getvm.py", stdin=_GETVM_SNIPPET, timeout=30)
    r = _host_ssh(
        key, host,
        f"PYTHONPATH=/opt/aleph-vm VM_ID={vm_hash} SUPERVISOR_SOCKET={socket_path} "
        "python3 /tmp/numa_getvm.py",
        timeout=60,
    )
    if r.returncode != 0:
        print(f"[numa] GetVm read unavailable (rc={r.returncode}); "
              f"skipping numa_node value check.\nstderr: {r.stderr}")
        return None
    for line in r.stdout.splitlines():
        if line.startswith("NUMA_NODE="):
            return line.split("=", 1)[1].strip()
    return None


@pytest.mark.timeout(1200)
def test_numa_placement_inert_on_single_node(
    aleph_cli, rootfs_hash, ssh_key_pair, crn_ssh_key,
):
    private_key_path, public_key_path = ssh_key_pair

    vm = create_dispatched_instance(
        aleph_cli, rootfs_hash, public_key_path, "numa-instance",
    )
    try:
        # Wait for the VM so the controller unit (and any drop-in) exists.
        wait_for_ssh(private_key_path, vm.crn_host, vm.ssh_port, timeout=180)

        host = vm.crn_host
        nodes = _numa_node_count(crn_ssh_key, host)
        impl = _supervisor_impl(crn_ssh_key, host)
        dropin = _dropin_present(crn_ssh_key, host, vm.hash)
        socket_path = _discover_socket(crn_ssh_key, host)
        numa_node = _read_numa_node(crn_ssh_key, host, vm.hash, socket_path)
        print(f"[numa] host={host} nodes={nodes} impl={impl} "
              f"dropin={dropin} numa_node={numa_node}")

        multi_node = nodes > 1

        if not multi_node:
            # The expected DO-runner path: single node -> everything inert.
            assert not dropin, (
                f"Single-node host {host} unexpectedly has a NUMA AllowedCPUs "
                f"drop-in for {vm.hash}:\n{_dropin_body(crn_ssh_key, host, vm.hash)}"
            )
            if numa_node is not None:
                assert numa_node == "none", (
                    f"Single-node host reported numa_node={numa_node!r}, expected none"
                )
            return

        # Multi-socket runner: pack-first only holds under the Rust supervisor.
        if impl != "rust":
            pytest.skip(
                f"Host {host} has {nodes} NUMA nodes but the {impl} supervisor is "
                "active; NUMA placement is Rust-only. Re-run with the candidate deb "
                "and ALEPH_VM_SUPERVISOR_IMPL=rust to assert pack-first."
            )
        assert dropin, (
            f"Multi-node host {host} under the rust supervisor did NOT write a "
            f"numa.conf drop-in for {vm.hash} (expected pack-first placement)."
        )
        body = _dropin_body(crn_ssh_key, host, vm.hash)
        assert "AllowedCPUs=" in body, f"numa.conf lacks AllowedCPUs=:\n{body}"
        assert numa_node not in (None, "none"), (
            f"Multi-node placement wrote a drop-in but numa_node is {numa_node!r}"
        )
    finally:
        delete_instance(aleph_cli, vm.hash)


# ── ASSUMPTIONS / UNKNOWNS ───────────────────────────────────────────────────
# N1. The DO CRN droplet is single-NUMA-node, so this test only ever exercises
#     the INERT C1 contract there. A genuine pack-first assertion needs a
#     multi-socket runner; the test upgrades itself to assert pack-first if it
#     lands on one, but that path is unexercised on DigitalOcean.
# N2. On a single-node host the inert result (no drop-in, numa_node None) holds
#     under EITHER supervisor impl, so the test does not force impl=rust and
#     stays valid even when it runs (alphabetically) before scenario A upgrades
#     the CRN to the candidate deb. To specifically exercise the Rust C1 code,
#     ensure the CRN carries the candidate deb with ALEPH_VM_SUPERVISOR_IMPL=rust
#     before this runs (e.g. baseline == candidate, or reordered after upgrade).
# N3. numa_node is read via on-host gRPC GetVm (the agent HTTP API always reports
#     None). That read needs the candidate deb; on a baseline deb it is skipped
#     and only the drop-in absence is asserted. The drop-in check alone is a
#     sufficient signal for the inert contract.
