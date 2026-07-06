"""aleph-vm upgrade/adoption checks: the only tests on this branch.

Scenario A (release upgrade): a CRN installed with the baseline release deb
hosts a running instance; the CRN is upgraded in place to the candidate deb
(CI artifact of ALEPH_VM_UPGRADE_BRANCH, default "dev") and the instance must
survive, port forwards included, with lifecycle operations still working.

Scenario B (supervisor impl swap): on a CRN running the candidate deb, flip
ALEPH_VM_SUPERVISOR_IMPL between python and rust (the swap procedure from the
Rust supervisor port design: stop unit, edit /etc/aleph-vm/supervisor.env,
start unit) and assert a running instance is untouched. Gated behind
UPGRADE_CHECK_RUST=1 until the Rust daemon serves the read-only RPC surface
(aleph-vm Rust port increment 2); UPGRADE_CHECK_RUST_LIFECYCLE=1 additionally
exercises stop/start while the Rust daemon is active (increment 3).

Both tests run where crn-up.sh ran (in CI: the CCN droplet). They reach the
CRN *host* over SSH as root with SSH_KEY_FILE (crn_ssh_key fixture) and drive
the mid-test deb upgrade through `scripts/crn-up.sh --upgrade`, which reuses
the deploy path's artifact-download code (gh CLI + GH_TOKEN required for
branch artifacts).
"""
import json
import os
import subprocess
import urllib.request
import uuid
from pathlib import Path

import pytest

from tests.vm_helpers import (
    create_dispatched_instance,
    delete_instance,
    poll,
    ssh_ok,
    ssh_run,
    wait_for_dispatched,
    wait_for_ssh,
)

REPO_ROOT = Path(__file__).resolve().parent.parent

# aleph-vm branch whose CI deb scenario A upgrades to (the upgrade candidate).
# Runs override this to the increment branch under test.
UPGRADE_BRANCH = os.environ.get("ALEPH_VM_UPGRADE_BRANCH", "dev")

RUST_SWAP_ENABLED = os.environ.get("UPGRADE_CHECK_RUST") == "1"
RUST_LIFECYCLE = os.environ.get("UPGRADE_CHECK_RUST_LIFECYCLE") == "1"

SUPERVISOR_ENV_FILE = "/etc/aleph-vm/supervisor.env"
MARKER_FILE = "/root/upgrade-marker.txt"


# ---------------------------------------------------------------------------
# CRN host helpers (root SSH to the node itself, not to a VM)
# ---------------------------------------------------------------------------

def _crn_run(crn_ssh_key, host, command, timeout=60):
    """Run a command as root on the CRN host; fail the test with full output
    on a non-zero exit."""
    try:
        return ssh_run(crn_ssh_key, host, 22, command, timeout=timeout)
    except subprocess.CalledProcessError as e:
        pytest.fail(
            f"Command on CRN host {host} failed (exit {e.returncode}): {command}\n"
            f"stdout: {e.stdout}\nstderr: {e.stderr}"
        )


def _aleph_vm_version(crn_ssh_key, host) -> str:
    # Single quotes so the remote shell passes ${Version} to dpkg-query verbatim.
    return _crn_run(crn_ssh_key, host, "dpkg-query -W -f '${Version}' aleph-vm").strip()


def _supervisor_exe(crn_ssh_key, host) -> str:
    """Resolved executable of the aleph-vm-supervisor MainPID.

    This is how we tell which implementation actually serves: the python
    daemon runs under .../pythonX.Y, the Rust daemon is its own binary.
    """
    pid = _crn_run(
        crn_ssh_key, host,
        "systemctl show -p MainPID --value aleph-vm-supervisor.service",
    ).strip()
    if not pid or pid == "0":
        pytest.fail(f"aleph-vm-supervisor has no MainPID on {host} (unit not running?)")
    return _crn_run(crn_ssh_key, host, f"readlink -f /proc/{pid}/exe").strip()


def _wait_supervisor_active(crn_ssh_key, host, context, timeout=120):
    def fetch():
        out = _crn_run(
            crn_ssh_key, host,
            "systemctl is-active aleph-vm-supervisor.service || true",
        ).strip()
        return out if out == "active" else None
    poll(f"aleph-vm-supervisor active ({context})", fetch, timeout=timeout)


def _set_supervisor_impl(crn_ssh_key, host, impl):
    """The documented swap procedure: stop the supervisor unit, flip
    ALEPH_VM_SUPERVISOR_IMPL in supervisor.env, start the unit.

    Persistent VMs live in their own aleph-vm-controller@ units and must not
    notice; their nftables port-forward rules are kernel state.
    """
    assert impl in ("python", "rust")
    _crn_run(crn_ssh_key, host, "systemctl stop aleph-vm-supervisor.service")
    # sed-or-append, same idiom crn-up.sh uses for ALEPH_VM_NODE_HASH.
    _crn_run(
        crn_ssh_key, host,
        f"grep -q '^ALEPH_VM_SUPERVISOR_IMPL=' {SUPERVISOR_ENV_FILE} "
        f"&& sed -i 's/^ALEPH_VM_SUPERVISOR_IMPL=.*/ALEPH_VM_SUPERVISOR_IMPL={impl}/' {SUPERVISOR_ENV_FILE} "
        f"|| echo 'ALEPH_VM_SUPERVISOR_IMPL={impl}' >> {SUPERVISOR_ENV_FILE}",
    )
    _crn_run(crn_ssh_key, host, "systemctl start aleph-vm-supervisor.service")
    _wait_supervisor_active(crn_ssh_key, host, context=f"impl={impl}")


def _assert_supervisor_impl(crn_ssh_key, host, impl):
    exe = _supervisor_exe(crn_ssh_key, host)
    looks_python = "python" in os.path.basename(exe)
    if impl == "python" and not looks_python:
        pytest.fail(
            f"Expected the python supervisor on {host} but MainPID exe is {exe}"
        )
    if impl == "rust" and looks_python:
        pytest.fail(
            f"ALEPH_VM_SUPERVISOR_IMPL=rust did not take effect on {host}: the "
            f"supervisor MainPID exe is still {exe}. Either the candidate deb "
            "does not ship the implementation launcher yet, or the env var is "
            "ignored. Re-run without UPGRADE_CHECK_RUST=1 until it does."
        )


def _run_crn_upgrade(*, branch=None, version=None):
    """Invoke scripts/crn-up.sh --upgrade with an explicit target.

    The script iterates over .local/crn/<idx> state dirs and exits non-zero
    if any CRN does not come back serving, so a failed upgrade fails here
    with the script's transcript attached.
    """
    env = {**os.environ}
    env.pop("ALEPH_VM_BRANCH", None)
    env.pop("ALEPH_VM_VERSION", None)
    if branch:
        env["ALEPH_VM_BRANCH"] = branch
    if version:
        env["ALEPH_VM_VERSION"] = version
    script = REPO_ROOT / "scripts" / "crn-up.sh"
    result = subprocess.run(
        ["bash", str(script), "--upgrade"],
        env=env, capture_output=True, text=True, timeout=1200,
    )
    # Keep the transcript in the pytest output for post-mortems.
    print(result.stdout)
    if result.returncode != 0:
        pytest.fail(
            f"crn-up.sh --upgrade failed (exit {result.returncode}).\n"
            f"stdout (tail):\n{result.stdout[-3000:]}\n"
            f"stderr (tail):\n{result.stderr[-3000:]}"
        )


# ---------------------------------------------------------------------------
# Shared assertions
# ---------------------------------------------------------------------------

def _write_marker(private_key_path, vm) -> str:
    """Persist a random marker inside the VM; `sync` so it hits the disk."""
    marker = uuid.uuid4().hex
    ssh_run(
        private_key_path, vm.crn_host, vm.ssh_port,
        f"echo {marker} > {MARKER_FILE} && sync",
    )
    return marker


def _read_marker(private_key_path, vm) -> str:
    return ssh_run(
        private_key_path, vm.crn_host, vm.ssh_port, f"cat {MARKER_FILE}",
    ).strip()


def _assert_marker(private_key_path, vm, marker, context):
    wait_for_ssh(private_key_path, vm.crn_host, vm.ssh_port, timeout=120)
    persisted = _read_marker(private_key_path, vm)
    assert persisted == marker, (
        f"VM disk state lost ({context}): expected marker {marker!r}, "
        f"got {persisted!r}"
    )


def _assert_stop_start_lifecycle(aleph_cli, vm, private_key_path, marker, context):
    """`instance stop` must take the VM offline, `instance start` must bring
    it back SSH-reachable with the marker intact. The mapped SSH port can
    change across a start, hence the refresh."""
    aleph_cli("instance", "stop", vm.hash, "--chain", "eth")
    poll(
        f"VM stopped, SSH unreachable ({context})",
        lambda: True if not ssh_ok(private_key_path, vm.crn_host, vm.ssh_port) else None,
        timeout=180,
    )
    aleph_cli("instance", "start", vm.hash, "--chain", "eth")
    vm.refresh(aleph_cli, timeout=300)
    _assert_marker(private_key_path, vm, marker, f"after stop/start, {context}")


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


def _crn_executions(crn_host) -> dict:
    """The CRN's public executions listing (v2), keyed by item hash."""
    url = f"http://{crn_host}:4020/v2/about/executions/list"
    with urllib.request.urlopen(url, timeout=10) as resp:
        return json.loads(resp.read())


# ---------------------------------------------------------------------------
# Scenario A: release deb -> candidate deb package upgrade
# ---------------------------------------------------------------------------

@pytest.mark.timeout(2400)
def test_release_upgrade_preserves_running_instance(
    aleph_cli, rootfs_hash, ssh_key_pair, crn_ssh_key,
):
    """Package upgrade under load, exactly like an operator's node would see it.

    Requires the CRN to have been installed with the baseline deb (in CI the
    workflow passes ALEPH_VM_VERSION=<release> to crn-up.sh --install). The
    upgrade target is the candidate branch's CI deb (ALEPH_VM_UPGRADE_BRANCH,
    default "dev").
    """
    private_key_path, public_key_path = ssh_key_pair

    # Baseline: a dispatched, SSH-reachable instance with a marker on disk
    # and an extra port forward beyond the default SSH mapping.
    vm = create_dispatched_instance(
        aleph_cli, rootfs_hash, public_key_path, "upgrade-a-instance",
    )
    try:
        wait_for_ssh(private_key_path, vm.crn_host, vm.ssh_port, timeout=120)
        marker = _write_marker(private_key_path, vm)

        vm_port = 8000
        aleph_cli("instance", "port-forward", "create", vm.hash, str(vm_port),
                  "--tcp", "true", "--chain", "eth")
        aleph_cli("instance", "port-forward", "refresh", vm.hash, "--chain", "eth")
        _wait_forward_host_port(aleph_cli, vm.hash, vm_port)

        version_before = _aleph_vm_version(crn_ssh_key, vm.crn_host)
        print(f"aleph-vm before upgrade: {version_before}")

        # The upgrade. crn-up.sh --upgrade already enforces that the
        # supervisor unit and the :4020 API come back.
        _run_crn_upgrade(branch=UPGRADE_BRANCH)

        version_after = _aleph_vm_version(crn_ssh_key, vm.crn_host)
        print(f"aleph-vm after upgrade: {version_after}")

        # The instance survived in place: same node, both port forwards still
        # mapped, marker still readable over SSH.
        show = wait_for_dispatched(aleph_cli, vm.hash, timeout=180)
        assert show["placement"]["allocated_node"] == vm.crn_hash, (
            f"Instance moved off its CRN across the upgrade: "
            f"{show['placement']['allocated_node']} != {vm.crn_hash}"
        )
        mapped = show.get("mapped_ports") or {}
        assert mapped.get(str(vm_port)) is not None, (
            f"Port forward for VM port {vm_port} lost across the upgrade "
            f"({version_before} -> {version_after}); mapped_ports: {mapped}"
        )
        vm.refresh(aleph_cli, timeout=120)
        _assert_marker(
            private_key_path, vm, marker,
            f"upgrade {version_before} -> {version_after}",
        )

        # Lifecycle still works on the upgraded node.
        _assert_stop_start_lifecycle(
            aleph_cli, vm, private_key_path, marker,
            context=f"post-upgrade, aleph-vm {version_after}",
        )
        mapped = (wait_for_dispatched(aleph_cli, vm.hash, timeout=120)
                  .get("mapped_ports") or {})
        assert mapped.get(str(vm_port)) is not None, (
            f"Port forward for VM port {vm_port} lost across post-upgrade "
            f"stop/start; mapped_ports: {mapped}"
        )
    finally:
        delete_instance(aleph_cli, vm.hash)


# ---------------------------------------------------------------------------
# Scenario B: ALEPH_VM_SUPERVISOR_IMPL python <-> rust swap
# ---------------------------------------------------------------------------

@pytest.mark.skipif(
    not RUST_SWAP_ENABLED,
    reason="Rust supervisor swap checks disabled; set UPGRADE_CHECK_RUST=1 "
           "once the Rust daemon serves the read-only RPC surface "
           "(aleph-vm Rust port increment 2)",
)
@pytest.mark.timeout(1800)
def test_supervisor_impl_swap_preserves_running_instance(
    aleph_cli, rootfs_hash, ssh_key_pair, crn_ssh_key,
):
    """Daemon swap under load, the Rust port's adoption path.

    Runs on a CRN already carrying the candidate deb: either the manifesto
    pinned it at install time, or scenario A (which runs first in this module)
    upgraded the CRN in place.

    Read-only phase (always): create an instance under impl=python, flip to
    rust, assert the instance survived (marker over SSH, executions listing
    still reports it running). With UPGRADE_CHECK_RUST_LIFECYCLE=1, also
    stop/start the instance while the Rust daemon is active. Finally swap back
    to python and assert lifecycle operations still work.
    """
    private_key_path, public_key_path = ssh_key_pair

    vm = create_dispatched_instance(
        aleph_cli, rootfs_hash, public_key_path, "upgrade-b-instance",
    )
    try:
        wait_for_ssh(private_key_path, vm.crn_host, vm.ssh_port, timeout=120)
        marker = _write_marker(private_key_path, vm)
        host = vm.crn_host

        # Pin impl=python explicitly so the starting state is what we claim
        # (the deb default is python; this also proves the VM shrugs off a
        # plain supervisor restart before we blame the rust swap for anything).
        _set_supervisor_impl(crn_ssh_key, host, "python")
        _assert_supervisor_impl(crn_ssh_key, host, "python")
        _assert_marker(private_key_path, vm, marker, "after supervisor restart under python")

        # Flip to rust.
        _set_supervisor_impl(crn_ssh_key, host, "rust")
        _assert_supervisor_impl(crn_ssh_key, host, "rust")

        # The VM must have survived: it runs in its own systemd unit and its
        # port forwards are kernel nftables state.
        _assert_marker(private_key_path, vm, marker, "after swap to rust")

        # Read-only surface: the CRN's executions listing must still report
        # the instance as running (served through the supervisor's ListVms).
        def listed_running():
            try:
                listing = _crn_executions(host)
            except Exception:
                return None
            entry = listing.get(vm.hash)
            if entry and entry.get("running") is True:
                return entry
            return None
        poll(
            f"executions listing reports {vm.hash[:12]} running under the rust daemon",
            listed_running, timeout=120,
        )

        if RUST_LIFECYCLE:
            _assert_stop_start_lifecycle(
                aleph_cli, vm, private_key_path, marker,
                context="under the rust daemon",
            )

        # Swap back to python; the rollback path must be just as boring.
        _set_supervisor_impl(crn_ssh_key, host, "python")
        _assert_supervisor_impl(crn_ssh_key, host, "python")
        _assert_marker(private_key_path, vm, marker, "after swap back to python")
        _assert_stop_start_lifecycle(
            aleph_cli, vm, private_key_path, marker,
            context="after swap back to python",
        )
    finally:
        delete_instance(aleph_cli, vm.hash)
