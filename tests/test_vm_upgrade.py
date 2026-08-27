"""aleph-vm in-place upgrade checks: 1.15.0 -> dev (2.0 release candidate).

Scenario A (release upgrade): CRNs installed with the BASELINE release deb
host running QEMU instances (a plain one, and, when the confidential
fixtures are configured, an AMD SEV one on the TEE server); every CRN is
upgraded in place to the CANDIDATE deb (CI artifact of
ALEPH_VM_UPGRADE_BRANCH, default "dev") and each instance must survive
untouched: same controller unit, same QEMU process, same disk, port forwards
included, with lifecycle operations still working afterwards and the VM
coming back on the SAME rootfs file after a stop/start.

Scenario B (supervisor impl swap): on an upgraded CRN, flip
ALEPH_VM_SUPERVISOR_IMPL between python and rust (stop unit, edit
/etc/aleph-vm/supervisor.env, start unit) and assert a running instance is
untouched, then stop/start it under the Rust daemon and after swapping back.
Runs once with a plain instance and, when configured, once with an SEV
instance on the TEE server. Set UPGRADE_CHECK_RUST=0 to skip both.

The tests run where crn-up.sh ran (in CI: the CCN droplet). They reach the
CRN *hosts* over SSH with SSH_KEY_FILE (crn_ssh_key fixture), as root or as
the per-CRN `ssh-user` recorded in .local/crn/<idx>/ (non-root users run
commands through passwordless sudo, like crn-up.sh does), and drive the
mid-test deb upgrade through `scripts/crn-up.sh --upgrade` (gh CLI +
GH_TOKEN required for branch artifacts; UPGRADE_STATIC=1 to include the
static TEE server).
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
    resolve_crn_host,
    ssh_ok,
    ssh_run,
    wait_for_dispatched,
    wait_for_ssh,
    DispatchedVM,
)

REPO_ROOT = Path(__file__).resolve().parent.parent

# aleph-vm branch whose CI deb scenario A upgrades to (the upgrade candidate).
# Set only by .github/workflows/upgrade-check.yml: the module is opt-in, so the
# regular pr-tests run (no GH_TOKEN, no candidate) does not fail on it.
UPGRADE_BRANCH = os.environ.get("ALEPH_VM_UPGRADE_BRANCH", "")

pytestmark = pytest.mark.skipif(
    not UPGRADE_BRANCH, reason="upgrade checks are opt-in: set ALEPH_VM_UPGRADE_BRANCH"
)

RUST_SWAP_ENABLED = os.environ.get("UPGRADE_CHECK_RUST", "1") == "1"

SUPERVISOR_ENV_FILE = "/etc/aleph-vm/supervisor.env"
MARKER_FILE = "/root/upgrade-marker.txt"
EXECUTION_ROOT = "/var/lib/aleph/vm"


# ---------------------------------------------------------------------------
# CRN host helpers (SSH to the node itself, not to a VM)
# ---------------------------------------------------------------------------

def _crn_ssh_user(host) -> str:
    """The SSH user crn-up.sh recorded for this CRN host (root by default)."""
    for state_dir in (REPO_ROOT / ".local" / "crn").glob("*/"):
        ip_file = state_dir / "droplet-ip"
        if ip_file.is_file() and ip_file.read_text().strip() == host:
            user_file = state_dir / "ssh-user"
            if user_file.is_file():
                return user_file.read_text().strip() or "root"
            return "root"
    return "root"


def _crn_run(crn_ssh_key, host, command, timeout=60):
    """Run a command as root on the CRN host; fail the test with full output
    on a non-zero exit. A non-root SSH user goes through `sudo -n`."""
    user = _crn_ssh_user(host)
    argv = [
        "ssh", "-i", crn_ssh_key, "-p", "22",
        "-o", "StrictHostKeyChecking=no", "-o", "UserKnownHostsFile=/dev/null",
        "-o", "ConnectTimeout=10",
        f"{user}@{host}",
    ]
    if user == "root":
        argv.append(command)
    else:
        argv += ["sudo", "-n", "bash", "-c", _shell_quote(command)]
    try:
        result = subprocess.run(argv, capture_output=True, text=True, timeout=timeout, check=True)
    except subprocess.CalledProcessError as e:
        pytest.fail(
            f"Command on CRN host {host} (as {user}) failed (exit {e.returncode}): {command}\n"
            f"stdout: {e.stdout}\nstderr: {e.stderr}"
        )
    return result.stdout


def _shell_quote(s: str) -> str:
    return "'" + s.replace("'", "'\\''") + "'"


def _aleph_vm_version(crn_ssh_key, host) -> str:
    # Single quotes so the remote shell passes ${Version} to dpkg-query verbatim.
    return _crn_run(crn_ssh_key, host, "dpkg-query -W -f '${Version}' aleph-vm").strip()


def _crn_utc_now(crn_ssh_key, host) -> str:
    """The CRN's own clock, in journalctl --since format."""
    return _crn_run(crn_ssh_key, host, "date -u '+%Y-%m-%d %H:%M:%S'").strip()


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


def _controller_snapshot(crn_ssh_key, host, vm_hash) -> dict:
    """Everything that must NOT change across an in-place upgrade.

    The controller unit's main process and start time (a restart would
    reboot the guest), the QEMU process(es) carrying the VM (exactly one:
    two would be a double boot), the vm_index persisted in the controller
    config (tap/IPv4/IPv6 derive from it) and the identity of the rootfs
    file the VM runs on (a re-created file would be a fresh disk).
    """
    unit = f"aleph-vm-controller@{vm_hash}.service"
    main_pid = _crn_run(
        crn_ssh_key, host, f"systemctl show -p MainPID --value {unit}",
    ).strip()
    started = _crn_run(
        crn_ssh_key, host, f"systemctl show -p ExecMainStartTimestamp --value {unit}",
    ).strip()
    # QEMU argv carries the VM's socket paths (<hash>-qmp.socket), so the hash
    # identifies the process. The [q] bracket keeps the remote shell running
    # this very command line out of the match.
    qemu_pids = _crn_run(
        crn_ssh_key, host,
        f"pgrep -f '[q]emu-system-x86_64.*{vm_hash}' || true",
    ).split()
    vm_index = _crn_run(
        crn_ssh_key, host,
        "python3 -c \"import json,sys; print(json.load(open(sys.argv[1]))['vm_id'])\" "
        f"{EXECUTION_ROOT}/{vm_hash}-controller.json",
    ).strip()
    rootfs_inode = _crn_run(
        crn_ssh_key, host,
        f"stat -c '%i %s' {EXECUTION_ROOT}/volumes/persistent/{vm_hash}/rootfs.qcow2",
    ).strip().split()[0]
    return {
        "main_pid": main_pid,
        "started": started,
        "qemu_pids": sorted(qemu_pids),
        "vm_index": vm_index,
        "rootfs_inode": rootfs_inode,
    }


def _assert_untouched(before: dict, after: dict, context: str):
    assert len(after["qemu_pids"]) == 1, (
        f"Expected exactly one QEMU process for the VM {context}, found "
        f"{after['qemu_pids']} (before: {before['qemu_pids']}); more than one "
        "means the VM was booted twice"
    )
    for key in ("main_pid", "started", "qemu_pids", "vm_index", "rootfs_inode"):
        assert before[key] == after[key], (
            f"{key} changed {context}: {before[key]!r} -> {after[key]!r}; "
            f"full snapshots before={before} after={after}"
        )


def _assert_no_adoption_errors(crn_ssh_key, host, since_ts):
    """The 2.0 supervisor re-adopts running controllers at startup. A
    failed/exhausted reattach leaves the VM running but unmanaged (not
    listed, not billed, not restartable), which the black-box checks above
    can miss for a while. Grep the daemon journals for it."""
    pattern = r"(reattach|re-adopt|adopt).*(fail|error|exhaust)|Failed to (restore|reattach|adopt)"
    hits = _crn_run(
        crn_ssh_key, host,
        "journalctl -u aleph-vm-supervisor.service -u aleph-vm-agent.service "
        f"--since '{since_ts}' --no-pager 2>/dev/null | grep -iE '{pattern}' || true",
    ).strip()
    tracebacks = _crn_run(
        crn_ssh_key, host,
        "journalctl -u aleph-vm-supervisor.service -u aleph-vm-agent.service "
        f"--since '{since_ts}' --no-pager 2>/dev/null | grep -c Traceback || true",
    ).strip()
    print(f"{host}: daemon journals since {since_ts}: {tracebacks} traceback(s)")
    assert not hits, f"Adoption errors in the daemon journals of {host} since {since_ts}:\n{hits}"


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

    # Unit "active" means the process spawned, not that the daemon bound its
    # socket: the agent proxies lifecycle calls to the supervisor socket and
    # answers 500 in the gap. Do not return until a supervisor-backed agent
    # request round-trips.
    def fetch():
        out = _crn_run(
            crn_ssh_key, host,
            "curl -sf -o /dev/null http://localhost:4020/about/executions/list && echo ok || true",
        ).strip()
        return out if out == "ok" else None

    poll(f"supervisor serving through the agent (impl={impl})", fetch, timeout=60)


def _assert_supervisor_impl(crn_ssh_key, host, impl):
    exe = _supervisor_exe(crn_ssh_key, host)
    looks_python = "python" in os.path.basename(exe)
    if impl == "python" and not looks_python:
        pytest.fail(f"Expected the python supervisor on {host} but MainPID exe is {exe}")
    if impl == "rust" and looks_python:
        pytest.fail(
            f"ALEPH_VM_SUPERVISOR_IMPL=rust did not take effect on {host}: the "
            f"supervisor MainPID exe is still {exe}."
        )


def _require_upgraded(crn_ssh_key, host):
    """Only a split-package (2.0) deb ships the implementation launcher; on
    a node scenario A failed to upgrade the swap cannot mean anything."""
    launcher = _crn_run(
        crn_ssh_key, host,
        "test -x /opt/aleph-vm/bin/supervisor-launcher && echo present || true",
    ).strip()
    if launcher != "present":
        pytest.fail(
            f"{host} runs aleph-vm {_aleph_vm_version(crn_ssh_key, host)} without "
            "/opt/aleph-vm/bin/supervisor-launcher: the node was not upgraded "
            "to the candidate deb (did scenario A fail before the upgrade?)"
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
        env=env, capture_output=True, text=True, timeout=1800,
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


def _assert_marker(private_key_path, vm, marker, context, timeout=120):
    wait_for_ssh(private_key_path, vm.crn_host, vm.ssh_port, timeout=timeout)
    persisted = _read_marker(private_key_path, vm)
    assert persisted == marker, (
        f"VM disk state lost ({context}): expected marker {marker!r}, "
        f"got {persisted!r}"
    )


def _wait_ssh_down(private_key_path, vm, context):
    poll(
        f"VM stopped, SSH unreachable ({context})",
        lambda: True if not ssh_ok(private_key_path, vm.crn_host, vm.ssh_port) else None,
        timeout=180,
    )


def _assert_stop_start_lifecycle(aleph_cli, vm, private_key_path, marker, context):
    """`instance stop` must take the VM offline, `instance start` must bring
    it back SSH-reachable with the marker intact. The mapped SSH port can
    change across a start, hence the refresh."""
    aleph_cli("instance", "stop", vm.hash, "--chain", "eth")
    _wait_ssh_down(private_key_path, vm, context)
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
    """The CRN's public executions listing, keyed by item hash."""
    url = f"http://{crn_host}:4020/about/executions/list"
    with urllib.request.urlopen(url, timeout=10) as resp:
        return json.loads(resp.read())


def _wait_listed(crn_host, vm_hash, context, timeout=120) -> dict:
    """The CRN must report the VM in its executions listing: after an
    upgrade that proves the new supervisor adopted the controller and the
    new agent sees it through the gRPC boundary."""
    def fetch():
        try:
            listing = _crn_executions(crn_host)
        except Exception:
            return None
        entry = listing.get(vm_hash)
        if entry is None:
            return None
        if entry.get("running") is False:
            return None
        return entry
    entry = poll(f"executions listing reports {vm_hash[:12]} ({context})", fetch, timeout=timeout)
    print(f"executions entry ({context}): {json.dumps(entry)[:500]}")
    return entry


# ---------------------------------------------------------------------------
# Confidential (AMD SEV) instance helpers, after tests/test_confidential.py
# ---------------------------------------------------------------------------

class Confidential:
    """The confidential fixtures bundled, so the SEV half of a scenario can
    be passed around (or be None when the TEE server is not configured)."""

    def __init__(self, rootfs_hash, firmware_hash, firmware, password, crn_host):
        self.rootfs_hash = rootfs_hash
        self.firmware_hash = firmware_hash
        self.firmware = firmware
        self.password = password
        self.crn_host = crn_host


@pytest.fixture(scope="module")
def confidential(request) -> "Confidential | None":
    """None when any confidential fixture would skip (no TEE server)."""
    names = ("confidential_rootfs_hash", "confidential_firmware_hash",
             "confidential_firmware", "confidential_password", "confidential_crn_host")
    try:
        values = [request.getfixturevalue(n) for n in names]
    except pytest.skip.Exception as e:
        print(f"SEV half of the upgrade checks disabled: {e}")
        return None
    return Confidential(*values)


def _wait_scheduler_sees_tee(scheduler_api_url, crn_host):
    """scheduler-rs only reschedules on VM deltas and node add/remove; a node
    capability appearing later does not trigger one. Create the SEV instance
    only once the scheduler shows the TEE node as confidential + IPv6."""
    def fetch():
        url = f"{scheduler_api_url}/api/v1/nodes"
        data = json.loads(urllib.request.urlopen(url, timeout=10).read())
        nodes = data.get("items", []) if isinstance(data, dict) else (data or [])
        for n in nodes:
            if crn_host in (n.get("address") or ""):
                ok = n.get("confidential_computing_enabled") and n.get("supports_ipv6")
                return n if ok else None
        return None
    poll("Scheduler sees TEE node as confidential-capable with IPv6", fetch,
         timeout=300, interval=10)


def _confidential_unlock(aleph_cli, vm_hash, conf: Confidential, context):
    """init-session (platform cert chain + session keys via sevctl) then
    start (launch-measurement validation + disk secret injection), retried
    until the CRN reports the measurement."""
    aleph_cli(
        "instance", "confidential", "init-session", vm_hash,
        "--keep-session", "--chain", "eth",
    )

    def try_start():
        r = aleph_cli(
            "instance", "confidential", "start", vm_hash,
            "--secret", conf.password,
            "--firmware-file", conf.firmware,
            "--chain", "eth",
            "--json",
            check=False,
        )
        if r.returncode == 0:
            return r
        raise RuntimeError(f"confidential start: {r.stderr.strip()[-500:]}")

    poll(f"Confidential start, measurement + secret injection ({context})",
         try_start, timeout=300, interval=10)


def _create_confidential_instance(aleph_cli, conf: Confidential, public_key_path,
                                  scheduler_api_url, name) -> DispatchedVM:
    _wait_scheduler_sees_tee(scheduler_api_url, conf.crn_host)
    result = aleph_cli(
        "instance", "create", name,
        "--image", conf.rootfs_hash,
        "--confidential",
        "--confidential-firmware", conf.firmware_hash,
        "--vcpus", "1",
        "--memory", "4GiB",
        "--disk-size", "4GiB",
        "--ssh-pubkey-file", public_key_path,
        "--chain", "eth",
        parse_json=True,
    )
    vm_hash = result["item_hash"]
    assert vm_hash, "Instance create should return an item_hash"
    # A confidential VM is placed before it starts; ports map only after
    # secret injection.
    data = wait_for_dispatched(aleph_cli, vm_hash, timeout=300, required_port=None)
    crn_hash = data["placement"]["allocated_node"]
    crn_host = resolve_crn_host(aleph_cli, crn_hash)
    assert crn_host == conf.crn_host, (
        f"Confidential instance landed on {crn_host}, expected the TEE server {conf.crn_host}"
    )
    _confidential_unlock(aleph_cli, vm_hash, conf, "initial boot")
    data = wait_for_dispatched(aleph_cli, vm_hash, timeout=300)
    return DispatchedVM(hash=vm_hash, crn_hash=crn_hash, crn_host=crn_host,
                        ssh_port=int(data["mapped_ports"]["22"]))


def _assert_sev_active(private_key_path, vm, context):
    dmesg = ssh_run(
        private_key_path, vm.crn_host, vm.ssh_port,
        "dmesg | grep -i 'Memory Encryption Features active' || true",
    )
    assert "SEV" in dmesg, f"Guest kernel does not report SEV active ({context}): {dmesg!r}"
    root_src = ssh_run(private_key_path, vm.crn_host, vm.ssh_port, "findmnt -no SOURCE /").strip()
    assert root_src.startswith("/dev/mapper/"), (
        f"Root filesystem is not dm-crypt mapped ({context}): {root_src!r}"
    )


def _assert_confidential_stop_start(aleph_cli, vm, conf, private_key_path, marker, context):
    """Stop, start, then the secret has to be injected again: a confidential
    guest cannot unlock its disk without a fresh session + measurement."""
    aleph_cli("instance", "stop", vm.hash, "--chain", "eth")
    _wait_ssh_down(private_key_path, vm, context)
    aleph_cli("instance", "start", vm.hash, "--chain", "eth")
    _confidential_unlock(aleph_cli, vm.hash, conf, context)
    vm.refresh(aleph_cli, timeout=300)
    _assert_marker(private_key_path, vm, marker, f"after stop/start, {context}", timeout=300)
    _assert_sev_active(private_key_path, vm, f"after stop/start, {context}")


# ---------------------------------------------------------------------------
# Scenario A: release deb -> candidate deb package upgrade
# ---------------------------------------------------------------------------

@pytest.mark.timeout(3600)
def test_release_upgrade_preserves_running_instances(
    aleph_cli, rootfs_hash, ssh_key_pair, crn_ssh_key, confidential, scheduler_api_url,
):
    """Package upgrade under load, exactly like an operator's node would see it.

    Requires the CRNs to have been installed with the baseline deb (in CI the
    workflow passes ALEPH_VM_VERSION=<release> to crn-up.sh --install). The
    upgrade target is the candidate branch's CI deb (ALEPH_VM_UPGRADE_BRANCH,
    default "dev"). One upgrade pass covers every CRN, so the plain and the
    SEV instance (when configured) are both live when it happens.
    """
    private_key_path, public_key_path = ssh_key_pair

    # Baseline: a dispatched, SSH-reachable instance with a marker on disk
    # and an extra port forward beyond the default SSH mapping.
    vm = create_dispatched_instance(
        aleph_cli, rootfs_hash, public_key_path, "upgrade-a-instance",
    )
    sev = None
    try:
        host = vm.crn_host
        wait_for_ssh(private_key_path, host, vm.ssh_port, timeout=120)
        marker = _write_marker(private_key_path, vm)

        vm_port = 8000
        aleph_cli("instance", "port-forward", "create", vm.hash, str(vm_port),
                  "--tcp", "true", "--chain", "eth")
        aleph_cli("instance", "port-forward", "refresh", vm.hash, "--chain", "eth")
        _wait_forward_host_port(aleph_cli, vm.hash, vm_port)

        # SEV half: a confidential instance, unlocked and SSH-reachable on
        # the TEE server, with its own marker.
        if confidential is not None:
            sev = _create_confidential_instance(
                aleph_cli, confidential, public_key_path, scheduler_api_url, "upgrade-a-sev",
            )
            wait_for_ssh(private_key_path, sev.crn_host, sev.ssh_port, timeout=300)
            _assert_sev_active(private_key_path, sev, "before upgrade")
            sev_marker = _write_marker(private_key_path, sev)

        hosts = {host}
        if sev is not None:
            hosts.add(sev.crn_host)
        versions_before = {h: _aleph_vm_version(crn_ssh_key, h) for h in hosts}
        print(f"aleph-vm before upgrade: {versions_before}")
        before = _controller_snapshot(crn_ssh_key, host, vm.hash)
        print(f"controller snapshot before upgrade: {before}")
        assert len(before["qemu_pids"]) == 1, f"baseline VM has {before['qemu_pids']} QEMU processes"
        if sev is not None:
            sev_before = _controller_snapshot(crn_ssh_key, sev.crn_host, sev.hash)
            print(f"SEV controller snapshot before upgrade: {sev_before}")
            assert len(sev_before["qemu_pids"]) == 1, f"SEV VM has {sev_before['qemu_pids']} QEMU processes"
        upgrade_ts = {h: _crn_utc_now(crn_ssh_key, h) for h in hosts}

        # The upgrade. crn-up.sh --upgrade already enforces that the
        # supervisor unit and the :4020 API come back on every CRN.
        _run_crn_upgrade(branch=UPGRADE_BRANCH)

        versions_after = {h: _aleph_vm_version(crn_ssh_key, h) for h in hosts}
        print(f"aleph-vm after upgrade: {versions_after}")
        for h in hosts:
            print(f"{h}: supervisor executable after upgrade: {_supervisor_exe(crn_ssh_key, h)}")
            assert versions_after[h] != versions_before[h], (
                f"the upgrade did not change the installed package on {h} ({versions_before[h]})"
            )

        # White-box: the controller unit, its QEMU process, the persisted
        # vm_index and the rootfs file are all the same objects as before,
        # for both VMs.
        after = _controller_snapshot(crn_ssh_key, host, vm.hash)
        print(f"controller snapshot after upgrade: {after}")
        _assert_untouched(before, after, f"across the upgrade on {host}")
        _wait_listed(host, vm.hash, f"after upgrade to {versions_after[host]}")
        _assert_no_adoption_errors(crn_ssh_key, host, upgrade_ts[host])
        if sev is not None:
            sev_after = _controller_snapshot(crn_ssh_key, sev.crn_host, sev.hash)
            print(f"SEV controller snapshot after upgrade: {sev_after}")
            _assert_untouched(sev_before, sev_after, f"across the upgrade on the TEE server {sev.crn_host}")
            _wait_listed(sev.crn_host, sev.hash, f"SEV VM after upgrade to {versions_after[sev.crn_host]}")
            _assert_no_adoption_errors(crn_ssh_key, sev.crn_host, upgrade_ts[sev.crn_host])

        # Black-box: same node, both port forwards still mapped, marker still
        # readable over SSH.
        show = wait_for_dispatched(aleph_cli, vm.hash, timeout=180)
        assert show["placement"]["allocated_node"] == vm.crn_hash, (
            f"Instance moved off its CRN across the upgrade: "
            f"{show['placement']['allocated_node']} != {vm.crn_hash}"
        )
        mapped = show.get("mapped_ports") or {}
        assert mapped.get(str(vm_port)) is not None, (
            f"Port forward for VM port {vm_port} lost across the upgrade; mapped_ports: {mapped}"
        )
        vm.refresh(aleph_cli, timeout=120)
        _assert_marker(private_key_path, vm, marker, "plain instance across the upgrade")
        if sev is not None:
            show = wait_for_dispatched(aleph_cli, sev.hash, timeout=180)
            assert show["placement"]["allocated_node"] == sev.crn_hash, (
                f"SEV instance moved off the TEE server across the upgrade: "
                f"{show['placement']['allocated_node']} != {sev.crn_hash}"
            )
            sev.refresh(aleph_cli, timeout=120)
            _assert_marker(private_key_path, sev, sev_marker, "SEV instance across the upgrade")
            _assert_sev_active(private_key_path, sev, "after upgrade")

        # Lifecycle still works on the upgraded nodes, and each VM comes back
        # on the same rootfs file (a fresh file would be an empty disk even
        # if some other path kept the marker readable).
        _assert_stop_start_lifecycle(
            aleph_cli, vm, private_key_path, marker,
            context=f"post-upgrade, aleph-vm {versions_after[host]}",
        )
        restarted = _controller_snapshot(crn_ssh_key, host, vm.hash)
        print(f"controller snapshot after post-upgrade stop/start: {restarted}")
        assert len(restarted["qemu_pids"]) == 1, f"VM has {restarted['qemu_pids']} QEMU processes after stop/start"
        assert restarted["rootfs_inode"] == before["rootfs_inode"], (
            "post-upgrade stop/start relaunched the VM on a different rootfs file: "
            f"inode {before['rootfs_inode']} -> {restarted['rootfs_inode']}"
        )
        assert restarted["vm_index"] == before["vm_index"], (
            f"vm_index changed across stop/start: {before['vm_index']} -> {restarted['vm_index']}"
        )
        mapped = (wait_for_dispatched(aleph_cli, vm.hash, timeout=120)
                  .get("mapped_ports") or {})
        assert mapped.get(str(vm_port)) is not None, (
            f"Port forward for VM port {vm_port} lost across post-upgrade "
            f"stop/start; mapped_ports: {mapped}"
        )
        if sev is not None:
            _assert_confidential_stop_start(
                aleph_cli, sev, confidential, private_key_path, sev_marker,
                context=f"SEV post-upgrade, aleph-vm {versions_after[sev.crn_host]}",
            )
            restarted = _controller_snapshot(crn_ssh_key, sev.crn_host, sev.hash)
            print(f"SEV controller snapshot after post-upgrade stop/start: {restarted}")
            assert len(restarted["qemu_pids"]) == 1
            assert restarted["rootfs_inode"] == sev_before["rootfs_inode"], (
                "post-upgrade stop/start relaunched the SEV VM on a different rootfs file: "
                f"inode {sev_before['rootfs_inode']} -> {restarted['rootfs_inode']}"
            )
            assert restarted["vm_index"] == sev_before["vm_index"]
    finally:
        delete_instance(aleph_cli, vm.hash)
        if sev is not None:
            delete_instance(aleph_cli, sev.hash)


# ---------------------------------------------------------------------------
# Scenario B: ALEPH_VM_SUPERVISOR_IMPL python <-> rust swap
# ---------------------------------------------------------------------------

def _swap_scenario(aleph_cli, crn_ssh_key, private_key_path, vm, marker, *,
                   stop_start, sev_check=None):
    """Shared body: pin python, flip to rust, assert untouched + listed, stop/
    start under rust, flip back to python, stop/start again."""
    host = vm.crn_host
    _require_upgraded(crn_ssh_key, host)

    # Pin impl=python explicitly so the starting state is what we claim
    # (the deb default is python; this also proves the VM shrugs off a
    # plain supervisor restart before we blame the rust swap for anything).
    _set_supervisor_impl(crn_ssh_key, host, "python")
    _assert_supervisor_impl(crn_ssh_key, host, "python")
    _assert_marker(private_key_path, vm, marker, "after supervisor restart under python")

    before = _controller_snapshot(crn_ssh_key, host, vm.hash)
    swap_ts = _crn_utc_now(crn_ssh_key, host)

    # Flip to rust.
    _set_supervisor_impl(crn_ssh_key, host, "rust")
    _assert_supervisor_impl(crn_ssh_key, host, "rust")

    # The VM must have survived: it runs in its own systemd unit and its
    # port forwards are kernel nftables state.
    _assert_untouched(before, _controller_snapshot(crn_ssh_key, host, vm.hash), "across the swap to rust")
    _assert_marker(private_key_path, vm, marker, "after swap to rust")
    _wait_listed(host, vm.hash, "under the rust daemon")
    _assert_no_adoption_errors(crn_ssh_key, host, swap_ts)
    if sev_check:
        sev_check("after swap to rust")

    stop_start("under the rust daemon")

    # Swap back to python; the rollback path must be just as boring.
    _set_supervisor_impl(crn_ssh_key, host, "python")
    _assert_supervisor_impl(crn_ssh_key, host, "python")
    _assert_marker(private_key_path, vm, marker, "after swap back to python")
    _wait_listed(host, vm.hash, "after swap back to python")
    stop_start("after swap back to python")


@pytest.mark.skipif(
    not RUST_SWAP_ENABLED,
    reason="Rust supervisor swap checks disabled (UPGRADE_CHECK_RUST=0)",
)
@pytest.mark.timeout(1800)
def test_supervisor_impl_swap_preserves_running_instance(
    aleph_cli, rootfs_hash, ssh_key_pair, crn_ssh_key,
):
    """Daemon swap under load, the Rust port's adoption path, with a plain
    instance. Runs on a CRN already carrying the candidate deb (scenario A,
    which runs first in this module, upgraded every CRN in place)."""
    private_key_path, public_key_path = ssh_key_pair

    vm = create_dispatched_instance(
        aleph_cli, rootfs_hash, public_key_path, "upgrade-b-instance",
    )
    try:
        wait_for_ssh(private_key_path, vm.crn_host, vm.ssh_port, timeout=120)
        marker = _write_marker(private_key_path, vm)

        def stop_start(context):
            _assert_stop_start_lifecycle(aleph_cli, vm, private_key_path, marker, context=context)

        _swap_scenario(aleph_cli, crn_ssh_key, private_key_path, vm, marker, stop_start=stop_start)
    finally:
        delete_instance(aleph_cli, vm.hash)


@pytest.mark.skipif(
    not RUST_SWAP_ENABLED,
    reason="Rust supervisor swap checks disabled (UPGRADE_CHECK_RUST=0)",
)
@pytest.mark.timeout(2400)
def test_supervisor_impl_swap_preserves_confidential_instance(
    aleph_cli, ssh_key_pair, crn_ssh_key, confidential, scheduler_api_url,
):
    """Same swap with an AMD SEV instance live on the TEE server: the Rust
    daemon must adopt a confidential controller (session files, policy) and
    restart it through its own confidential launch path."""
    if confidential is None:
        pytest.skip("No TEE server configured; SEV swap check skipped")
    private_key_path, public_key_path = ssh_key_pair

    sev = _create_confidential_instance(
        aleph_cli, confidential, public_key_path, scheduler_api_url, "upgrade-b-sev",
    )
    try:
        wait_for_ssh(private_key_path, sev.crn_host, sev.ssh_port, timeout=300)
        marker = _write_marker(private_key_path, sev)

        def stop_start(context):
            _assert_confidential_stop_start(
                aleph_cli, sev, confidential, private_key_path, marker, context=context,
            )

        def sev_check(context):
            _assert_sev_active(private_key_path, sev, context)

        _swap_scenario(aleph_cli, crn_ssh_key, private_key_path, sev, marker,
                       stop_start=stop_start, sev_check=sev_check)
    finally:
        delete_instance(aleph_cli, sev.hash)
