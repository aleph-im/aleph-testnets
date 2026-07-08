"""End-to-end SEV-SNP measured-boot attestation check (Phase 3 increment D).

Unlike every other test on this branch, the SNP VM is created DIRECTLY over the
supervisor gRPC socket, not through `aleph instance create`: in-protocol SNP
scheduling is deferred, so there is no CLI/CCN path to an SNP VM yet. The flow:

  1. Ensure the SNP (TEE) host runs the RUST supervisor (ALEPH_VM_SUPERVISOR_IMPL
     =rust): the SEV-SNP launch path lives in the Rust crates (aleph-tee), the
     Python supervisor does not serve it.
  2. On the SNP host, over root SSH, run a small Python snippet that imports the
     candidate deb's GrpcSupervisor (/opt/aleph-vm on PYTHONPATH) and CreateVm an
     SNP VM: backend=QEMU, tee.backend=SEV_SNP, measured OVMF/kernel/initrd, a
     dm-verity rootfs (ROOTFS) + its hash tree (EXTRA), 2 vCPUs, matching memory.
  3. Take the guest's tap IPv4 from the returned VmInfo and run aleph-attest-cli
     ON THE SNP HOST against https://<guest-ip>:8443 (the guest tap IP is only
     routable from the host, hence local-to-guest). `attest` pins the launch
     measurement; `fresh-attest` re-verifies with a random nonce.
  4. DeleteVm over gRPC to clean up.

Everything runs on the SNP host: the measured image was pushed there by
scripts/snp-artifacts.sh, and aleph-attest-cli was staged next to it. The test
skips cleanly unless the SNP host and its measurement are configured (a normal
run without an AMD SEV-SNP server is unaffected).

See the ASSUMPTIONS block at the bottom: several details here (guest DHCP IP,
--amd-product for a Siena host, msgpack stubbing, the socket path) are explicit
guesses to confirm against real hardware.
"""
import os
import shlex
import subprocess
import time
import uuid

import pytest

from tests.vm_helpers import poll

# ---------------------------------------------------------------------------
# Configuration (all sourced from the environment local-up.sh exports).
# ---------------------------------------------------------------------------

# The SNP (TEE) host: reuses the confidential-CRN-host resolution — run_tests
# exports ALEPH_TESTNET_CONFIDENTIAL_CRN_HOST from the CRN state dir carrying
# the `confidential` marker, which for this workflow is the static SNP server.
SNP_HOST = os.environ.get("ALEPH_TESTNET_CONFIDENTIAL_CRN_HOST", "")

# SSH user on the SNP host. The static SNP server disallows root SSH login (only
# AMD_SEV_SNP_USER), so we connect as that user and elevate with `sudo -n` for
# every remote command, exactly like scripts/snp-artifacts.sh's snp_ssh. Default
# root for hosts that do allow it. See ASSUMPTIONS A2.
SNP_USER = os.environ.get("ALEPH_TESTNET_SNP_USER", "root")

# 48-byte SHA-384 launch measurement (hex), written by snp-artifacts.sh from the
# nix `measurement` output. The client pins this; a guest running anything else
# fails the handshake.
SNP_MEASUREMENT = os.environ.get("ALEPH_TESTNET_SNP_MEASUREMENT", "")

# Where snp-artifacts.sh placed the measured image + the attest CLI on the host.
SNP_IMAGE_HOST_DIR = os.environ.get("ALEPH_TESTNET_SNP_IMAGE_HOST_DIR", "/opt/aleph-snp-image")
SNP_ATTEST_CLI = os.environ.get(
    "ALEPH_TESTNET_SNP_ATTEST_CLI", f"{SNP_IMAGE_HOST_DIR}/aleph-attest-cli"
)

# AMD product for the KDS/ARK certificate-chain validation. The SNP host is an
# EPYC 8024P (Siena, 4th gen Zen4). We try "Genoa" first (also Zen4); if the KDS
# chain step fails, Siena likely needs its own ARK added client-side in
# aleph-tee (a follow-up in the aleph-vm repo, NOT fixable here). See ASSUMPTIONS.
SNP_AMD_PRODUCT = os.environ.get("ALEPH_TESTNET_SNP_AMD_PRODUCT", "Genoa")

# vCPUs + memory. The launch measurement is a function of (OVMF+kernel+initrd+
# cmdline + vCPU count + vCPU type); the nix default `measurement` output is
# 2 vCPUs / EPYC-v4 (Genoa), so vCPUs MUST be 2 to match. Memory does not enter
# the measurement, so 2048 MiB is a free choice (documented in ASSUMPTIONS).
SNP_VCPUS = os.environ.get("ALEPH_TESTNET_SNP_VCPUS", "2")
SNP_MEMORY_MIB = os.environ.get("ALEPH_TESTNET_SNP_MEMORY_MIB", "2048")

# Default SEV-SNP guest policy (aleph-tee DEFAULT_POLICY = "0x30000").
SNP_POLICY = os.environ.get("ALEPH_TESTNET_SNP_POLICY", "0x30000")

# Supervisor gRPC socket on the host. DISCOVERED at runtime from supervisor.env
# (ALEPH_VM_SUPERVISOR_GRPC_SOCKET) with the documented default as fallback:
# conf.py derives SUPERVISOR_GRPC_SOCKET = EXECUTION_ROOT/supervisor.sock, and
# EXECUTION_ROOT defaults to /var/lib/aleph/vm.
DEFAULT_SUPERVISOR_SOCKET = "/var/lib/aleph/vm/supervisor.sock"

SUPERVISOR_ENV_FILE = "/etc/aleph-vm/supervisor.env"

pytestmark = pytest.mark.skipif(
    not (SNP_HOST and SNP_MEASUREMENT),
    reason="SNP host or measurement not configured (needs AMD_SEV_SNP_HOST + the "
    "snp-artifacts.sh staging; set via the upgrade-checks workflow SNP path).",
)


# ---------------------------------------------------------------------------
# SNP-host SSH helpers (root on the host itself, not a VM).
# ---------------------------------------------------------------------------
# The test SSHes to the SNP host as root, reusing crn_ssh_key. If AMD_SEV_SNP_USER
# is not root this needs the sudo treatment scripts/*.sh already have — see
# ASSUMPTIONS A2.

def _ssh_cmd(key, host):
    return [
        "ssh", "-i", key,
        "-o", "StrictHostKeyChecking=no",
        "-o", "UserKnownHostsFile=/dev/null",
        "-o", "ConnectTimeout=10",
        f"{SNP_USER}@{host}",
    ]


def _snp_run(key, host, command, *, timeout=120, stdin=None):
    """Run a command on the SNP host; return CompletedProcess (no auto-fail).

    For a non-root SNP_USER the command is elevated with `sudo -n` (the SNP host
    forbids root SSH; every operation here needs root: systemctl, editing
    supervisor.env, the root-owned gRPC socket). stdin is piped through to the
    elevated command (e.g. `cat >` in _snp_put, or python reading a heredoc).
    """
    if SNP_USER != "root":
        command = "sudo -n bash -c " + shlex.quote(command)
    return subprocess.run(
        _ssh_cmd(key, host) + [command],
        input=stdin, capture_output=True, text=True, timeout=timeout,
    )


def _snp_put(key, host, remote_path, text):
    """Write `text` to `remote_path` on the SNP host via `cat >`."""
    result = _snp_run(key, host, f"cat > {remote_path}", stdin=text, timeout=30)
    if result.returncode != 0:
        pytest.fail(f"Failed to stage {remote_path} on {host}: {result.stderr}")


def _discover_socket(key, host) -> str:
    """Discover the supervisor gRPC socket path on the host, don't guess silently.

    Prefer the explicit ALEPH_VM_SUPERVISOR_GRPC_SOCKET from supervisor.env; fall
    back to the conf.py default EXECUTION_ROOT/supervisor.sock.
    """
    result = _snp_run(
        key, host,
        f"grep -E '^ALEPH_VM_SUPERVISOR_GRPC_SOCKET=' {SUPERVISOR_ENV_FILE} "
        "| tail -1 | cut -d= -f2- || true",
        timeout=30,
    )
    configured = (result.stdout or "").strip()
    socket_path = configured or DEFAULT_SUPERVISOR_SOCKET
    print(f"[snp] supervisor gRPC socket: {socket_path} "
          f"({'from supervisor.env' if configured else 'conf.py default'})")
    return socket_path


def _ensure_rust_supervisor(key, host, socket_path):
    """Ensure the SNP host serves the Rust supervisor and its gRPC socket is up.

    SEV-SNP is Rust-only, so we set ALEPH_VM_SUPERVISOR_IMPL=rust (idempotent,
    the documented swap: stop unit, edit supervisor.env, start unit) and wait for
    the socket to appear. Requires the candidate deb (rust daemon) to be
    installed on the host, which the workflow's SNP install step handles.
    """
    _snp_run(key, host, "systemctl stop aleph-vm-supervisor.service", timeout=60)
    set_impl = (
        f"grep -q '^ALEPH_VM_SUPERVISOR_IMPL=' {SUPERVISOR_ENV_FILE} "
        f"&& sed -i 's/^ALEPH_VM_SUPERVISOR_IMPL=.*/ALEPH_VM_SUPERVISOR_IMPL=rust/' {SUPERVISOR_ENV_FILE} "
        f"|| echo 'ALEPH_VM_SUPERVISOR_IMPL=rust' >> {SUPERVISOR_ENV_FILE}"
    )
    res = _snp_run(key, host, set_impl, timeout=30)
    if res.returncode != 0:
        pytest.fail(f"Could not set ALEPH_VM_SUPERVISOR_IMPL=rust on {host}: {res.stderr}")
    start = _snp_run(key, host, "systemctl start aleph-vm-supervisor.service", timeout=60)
    if start.returncode != 0:
        pytest.fail(
            f"aleph-vm-supervisor failed to start under impl=rust on {host}. "
            f"Is the candidate deb (rust daemon) installed?\nstderr: {start.stderr}"
        )

    def socket_ready():
        r = _snp_run(key, host, f"test -S {socket_path} && echo ok || true", timeout=30)
        return "ok" if r.stdout.strip() == "ok" else None

    poll(f"supervisor gRPC socket {socket_path} present on {host}",
         socket_ready, timeout=90, interval=3)


# ---------------------------------------------------------------------------
# On-host Python snippets driving the deb's GrpcSupervisor.
# ---------------------------------------------------------------------------
# Run with `PYTHONPATH=/opt/aleph-vm python3 <file>` so the deb's bundled
# aleph.vm package + its vendored grpc stack are importable.
#
# msgpack stub: the candidate deb's packaging/Makefile pip --target list omits
# msgpack (pyproject *does* list it — a packaging gap to fix upstream). client.py
# imports msgpack at module top, but only run_program_code uses it; the SNP
# create/get/delete path never does, so a stub module satisfies the import. See
# ASSUMPTIONS A4.

_MSGPACK_STUB = (
    "import sys, types\n"
    "try:\n"
    "    import msgpack  # noqa: F401\n"
    "except ModuleNotFoundError:\n"
    "    sys.modules['msgpack'] = types.ModuleType('msgpack')\n"
)

_CREATE_SNIPPET = _MSGPACK_STUB + '''
import asyncio, os
from pathlib import Path
from aleph.vm.supervisor_interface.client import GrpcSupervisor
from aleph.vm.supervisor_interface.types import (
    Backend, CreateVmSpec, DirectoryPath, DiskFormat, DiskRole, DiskSpec,
    NetworkConfig, TeeBackend, TeeConfig, VmId,
)

IMG = Path(os.environ["SNP_IMAGE_HOST_DIR"])
spec = CreateVmSpec(
    vm_id=VmId(os.environ["SNP_VM_ID"]),
    backend=Backend.QEMU,
    kernel_path=IMG / "bzImage",
    initrd_path=IMG / "initrd",
    # Disk ORDER is load-bearing: the guest init.sh reads the rootfs from the
    # first virtio disk (/dev/vda) and the dm-verity hash tree from /dev/vdb.
    disks=[
        DiskSpec(path=IMG / "rootfs.ext4", readonly=True,
                 format=DiskFormat.RAW, role=DiskRole.ROOTFS),
        DiskSpec(path=IMG / "rootfs.ext4.verity", readonly=True,
                 format=DiskFormat.RAW, role=DiskRole.EXTRA),
    ],
    vcpus=int(os.environ["SNP_VCPUS"]),
    memory_mib=int(os.environ["SNP_MEMORY_MIB"]),
    tee=TeeConfig(
        backend=TeeBackend.SEV_SNP,
        policy=os.environ["SNP_POLICY"],
        # The daemon derives its own CONFIDENTIAL_SESSION_DIRECTORY/<vm_id> for
        # SNP (lifecycle.rs), so this value is a required-but-ignored placeholder.
        session_dir=DirectoryPath(Path("/var/lib/aleph/vm/sessions")),
        firmware_path=IMG / "OVMF.fd",
    ),
    network=NetworkConfig(internet_access=True, requested_ipv6="", ipv6_prefix_len=0),
    gpus=[],
    numa_node=None,
    # QEMU instances start under systemd only; the daemon rejects a
    # non-persistent QEMU VM ("AlephQemuInstance starts under systemd only").
    persistent=True,
    hostname="snp-attest",
)

async def main():
    sup = GrpcSupervisor(os.environ["SUPERVISOR_SOCKET"])
    try:
        info = await sup.create_vm(spec)
        print(f"VM_ID={info.vm_id}")
        print(f"GUEST_IP={info.ipv4.address}")
        print(f"STATUS={info.status.value}")
        print(f"CONFIDENTIAL_MODE={info.confidential_mode.value}")
    finally:
        await sup.close()

asyncio.run(main())
'''

_DELETE_SNIPPET = _MSGPACK_STUB + '''
import asyncio, os
from aleph.vm.supervisor_interface.client import GrpcSupervisor
from aleph.vm.supervisor_interface.types import VmId

async def main():
    sup = GrpcSupervisor(os.environ["SUPERVISOR_SOCKET"])
    try:
        await sup.delete_vm(VmId(os.environ["SNP_VM_ID"]), wipe=True)
        print("DELETED")
    finally:
        await sup.close()

asyncio.run(main())
'''


def _run_host_python(key, host, script_path, env, *, timeout):
    """Run a staged on-host python script with the deb on PYTHONPATH."""
    env_prefix = " ".join(f"{k}={v}" for k, v in env.items())
    command = f"PYTHONPATH=/opt/aleph-vm {env_prefix} python3 {script_path}"
    return _snp_run(key, host, command, timeout=timeout)


def _parse_kv(stdout):
    out = {}
    for line in stdout.splitlines():
        if "=" in line:
            k, _, v = line.partition("=")
            out[k.strip()] = v.strip()
    return out


# ---------------------------------------------------------------------------
# The test.
# ---------------------------------------------------------------------------

@pytest.mark.timeout(1800)
def test_snp_measured_vm_attests(crn_ssh_key):
    """CreateVm an SNP VM over gRPC, verify its attestation report, DeleteVm."""
    host = SNP_HOST
    print(f"[snp] host={host} measurement={SNP_MEASUREMENT} product={SNP_AMD_PRODUCT}")

    # Sanity: the staged image + CLI must be on the host.
    check = _snp_run(
        crn_ssh_key, host,
        f"test -f {SNP_IMAGE_HOST_DIR}/bzImage "
        f"&& test -f {SNP_IMAGE_HOST_DIR}/OVMF.fd "
        f"&& test -x {SNP_ATTEST_CLI} && echo ok || true",
        timeout=30,
    )
    if check.stdout.strip() != "ok":
        pytest.fail(
            f"SNP artifacts missing on {host}: expected the measured image under "
            f"{SNP_IMAGE_HOST_DIR} and {SNP_ATTEST_CLI}. Did snp-artifacts.sh run?"
        )

    socket_path = _discover_socket(crn_ssh_key, host)
    _ensure_rust_supervisor(crn_ssh_key, host, socket_path)

    # A short, unique vm_id (item-hash-shaped: 64 hex). Bounds the socket path
    # length the supervisor derives (EXECUTION_ROOT/<id>-monitor.socket).
    vm_id = uuid.uuid4().hex + uuid.uuid4().hex  # 64 hex chars

    common_env = {
        "SNP_IMAGE_HOST_DIR": SNP_IMAGE_HOST_DIR,
        "SNP_VM_ID": vm_id,
        "SNP_VCPUS": SNP_VCPUS,
        "SNP_MEMORY_MIB": SNP_MEMORY_MIB,
        "SNP_POLICY": SNP_POLICY,
        "SUPERVISOR_SOCKET": socket_path,
    }

    _snp_put(crn_ssh_key, host, "/tmp/snp_create.py", _CREATE_SNIPPET)
    _snp_put(crn_ssh_key, host, "/tmp/snp_delete.py", _DELETE_SNIPPET)

    created = False
    try:
        # CreateVm. LIFECYCLE_TIMEOUT_SECS on the client is 300s; give SSH more.
        create = _run_host_python(
            crn_ssh_key, host, "/tmp/snp_create.py", common_env, timeout=420,
        )
        print("[snp] CreateVm stdout:\n" + create.stdout)
        print("[snp] CreateVm stderr:\n" + create.stderr)
        if create.returncode != 0:
            pytest.fail(
                f"CreateVm (SNP) failed on {host} (exit {create.returncode}).\n"
                f"stdout:\n{create.stdout}\nstderr:\n{create.stderr}"
            )
        created = True

        fields = _parse_kv(create.stdout)
        guest_ip = fields.get("GUEST_IP", "")
        assert guest_ip, f"CreateVm returned no guest IP; fields={fields}"
        assert fields.get("CONFIDENTIAL_MODE") == "sev_snp", (
            f"Expected confidential_mode=sev_snp, got {fields.get('CONFIDENTIAL_MODE')!r}"
        )
        print(f"[snp] guest IP: {guest_ip}")

        url = f"https://{guest_ip}:8443"
        attest_base = (
            f"{SNP_ATTEST_CLI} {{sub}} --url {url} "
            f"--expected-measurement {SNP_MEASUREMENT} --amd-product {SNP_AMD_PRODUCT}"
        )

        # The guest needs time to boot (DHCP, dm-verity, start attest-agent), so
        # poll `attest` until it succeeds. attest-cli exits non-zero on any
        # verification failure (KDS chain, measurement pin, key binding), so a
        # zero exit IS the assertion.
        last = {}

        def attest_ok():
            r = _snp_run(crn_ssh_key, host, attest_base.format(sub="attest"), timeout=120)
            last["r"] = r
            print(f"[snp] attest rc={r.returncode}\n{r.stdout}\n{r.stderr}")
            return r if r.returncode == 0 else None

        attest = poll(
            f"aleph-attest-cli attest against {url} on the SNP host",
            attest_ok, timeout=420, interval=15,
        )
        assert "Attestation valid: true" in attest.stdout, (
            f"attest-cli did not report a valid attestation:\n{attest.stdout}"
        )
        assert SNP_MEASUREMENT in attest.stdout.replace(" ", ""), (
            f"attest-cli reported a measurement other than the pinned one:\n{attest.stdout}"
        )

        # Layer 3: fresh attestation with a random nonce (generated inside the
        # CLI). Must also verify against the same measurement pin.
        fresh = _snp_run(
            crn_ssh_key, host, attest_base.format(sub="fresh-attest"), timeout=120,
        )
        print(f"[snp] fresh-attest rc={fresh.returncode}\n{fresh.stdout}\n{fresh.stderr}")
        assert fresh.returncode == 0, (
            f"fresh-attest failed (exit {fresh.returncode}):\n{fresh.stdout}\n{fresh.stderr}"
        )
        assert "verified successfully" in fresh.stdout, (
            f"fresh-attest did not confirm verification:\n{fresh.stdout}"
        )
    finally:
        if created:
            delete = _run_host_python(
                crn_ssh_key, host, "/tmp/snp_delete.py",
                {"SNP_VM_ID": vm_id, "SUPERVISOR_SOCKET": socket_path},
                timeout=300,
            )
            print(f"[snp] DeleteVm rc={delete.returncode}\n{delete.stdout}\n{delete.stderr}")


# ── ASSUMPTIONS / UNKNOWNS (confirm before / during the first run) ───────────
# A1. --amd-product Genoa on a Siena (EPYC 8024P) host: both are Zen4, so the
#     Genoa cert product MAY validate. If the KDS/ARK chain step fails, Siena
#     needs its own ARK added client-side in aleph-tee — a follow-up in the
#     aleph-vm repo, NOT fixable in aleph-testnets. Override with
#     ALEPH_TESTNET_SNP_AMD_PRODUCT if aleph-tee learns a Siena product name.
# A2. SNP host SSH is assumed to be root (ssh_run / _ssh_cmd hardcode root@). If
#     AMD_SEV_SNP_USER is non-root, these helpers need sudo like the *.sh scripts.
# A3. Guest IP: the measured kernel cmdline has NO ip= (it is fixed and hashed
#     into the measurement), so the guest init.sh falls back to udhcpc. We take
#     VmInfo.ipv4.address (the supervisor's tap assignment) as the guest IP and
#     assume the host serves DHCP on the tap so the guest actually claims it. If
#     the guest never appears at that IP, the attest poll times out; the guest
#     serial console is in the workflow's tee-controllers.txt artifact.
# A4. msgpack is stubbed because the candidate deb omits it (packaging/Makefile
#     pip --target list vs pyproject). If a later deb ships msgpack, the stub is
#     a harmless no-op (the try/import succeeds first).
# A5. vCPUs=2 + memory=2048MiB: the nix `measurement` output pins 2 vCPUs /
#     EPYC-v4 (Genoa). vCPUs MUST stay 2 or the measurement mismatches; memory is
#     free (not measured). The supervisor must also launch the guest with an
#     EPYC-v4-compatible CPU model, else the measurement diverges.
# A6. Rust supervisor: the test flips the SNP host to ALEPH_VM_SUPERVISOR_IMPL=
#     rust because SEV-SNP is Rust-only. This assumes the candidate deb (rust
#     daemon) is installed on the SNP host (the workflow's SNP install step).
# A7. Socket path is DISCOVERED from supervisor.env, falling back to the
#     conf.py default /var/lib/aleph/vm/supervisor.sock (EXECUTION_ROOT/
#     supervisor.sock). tee-reset.sh wipes /var/lib/aleph/vm/* but the daemon
#     recreates the socket on start.
