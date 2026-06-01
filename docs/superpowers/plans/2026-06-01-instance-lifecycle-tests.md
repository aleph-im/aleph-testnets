# Instance Lifecycle Integration Tests — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add integration tests for the `aleph instance` post-creation lifecycle (logs, port-forward, reboot, stop/start, backup) and extract the shared VM helpers duplicated across the existing instance tests.

**Architecture:** A new `tests/vm_helpers.py` holds the previously-duplicated polling/SSH/dispatch helpers plus a `DispatchedVM` handle and a `create_dispatched_instance` bootstrap. A session `rootfs_hash` fixture uploads the rootfs once. The two existing test files are refactored onto these. New tests live in `test_instance_lifecycle.py` (one shared module VM for non-destructive ops) and `test_instance_backup.py` (its own VM). `pytest-xdist` enables running modules concurrently.

**Tech Stack:** Python, pytest, pytest-timeout, pytest-xdist, the `aleph` CLI, SSH, urllib.

> **Verification note:** These tests require a live testnet (CCN + CRNs + rootfs) that is not available in the dev environment. Per-task verification is `python -m py_compile <files>` for syntax and (where an env exists) `pytest --collect-only`. The authoritative check is a live run with `CRN_COUNT>=2`, run by the operator. Commit messages omit the `Co-Authored-By: Claude` trailer per project preference.

---

### Task 1: Shared helpers module `tests/vm_helpers.py`

**Files:**
- Create: `tests/vm_helpers.py`

- [ ] **Step 1: Write the module**

```python
"""Shared helpers for instance/VM integration tests.

SSH reaches the VM over the CRN's IPv4 + the host-side mapped port reported by
`instance show --verbose` (`mapped_ports`). The CLI's own `instance ssh` uses
the VM's IPv6, which is not reachable in this CI.
"""
import subprocess
import time
from dataclasses import dataclass
from urllib.parse import urlparse

import pytest

# Nodestatus account: owns the testnet's corechannel aggregate (registered CRNs).
NODESTATUS_ADDR = "0x70997970C51812dc3A010C7d01b50e0d17dc79C8"


def poll(description, fetch, timeout, interval=5):
    """Poll fetch() until it returns a truthy value or timeout is reached."""
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


def resolve_crn_host(aleph_cli, crn_hash: str) -> str:
    """Return the CRN's reachable hostname (from its corechannel-registered URL)."""
    nodes = aleph_cli(
        "node", "list", "--type", "crn", "--all",
        "--corechannel-address", NODESTATUS_ADDR,
        parse_json=True,
    )
    for n in nodes or []:
        if n.get("hash") == crn_hash:
            return urlparse(n["address"]).hostname
    pytest.fail(f"CRN {crn_hash} not found in corechannel aggregate")


def _ssh_base(private_key_path, host, port):
    return [
        "ssh",
        "-i", private_key_path,
        "-p", str(port),
        "-o", "StrictHostKeyChecking=no",
        "-o", "UserKnownHostsFile=/dev/null",
        "-o", "ConnectTimeout=5",
        f"root@{host}",
    ]


def ssh_run(private_key_path, host, port, command, timeout=15):
    """Run a one-shot command over SSH; return stdout. Raises on non-zero exit."""
    result = subprocess.run(
        _ssh_base(private_key_path, host, port) + [command],
        capture_output=True, text=True, timeout=timeout, check=True,
    )
    return result.stdout


def ssh_ok(private_key_path, host, port, timeout=15) -> bool:
    """Return True if a one-shot `echo hello` SSH round-trip succeeds."""
    try:
        result = subprocess.run(
            _ssh_base(private_key_path, host, port) + ["echo hello"],
            capture_output=True, text=True, timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return False
    return result.returncode == 0 and "hello" in result.stdout


def wait_for_ssh(private_key_path, host, port, timeout=60):
    """Poll SSH until `echo hello` succeeds on host:port."""
    return poll(
        f"SSH into {host}:{port}",
        lambda: True if ssh_ok(private_key_path, host, port) else None,
        timeout=timeout,
    )


def wait_for_dispatched(aleph_cli, vm_hash, timeout=300, *, different_from=None, required_port="22"):
    """Poll `instance show --verbose` until the VM is dispatched with the named
    host-side mapped port present. If `different_from` is set, also require the
    allocated node hash to differ (used to detect post-unlink migration)."""
    def fetch():
        data = aleph_cli(
            "instance", "show", vm_hash, "--verbose",
            parse_json=True, check=False,
        )
        if not isinstance(data, dict):
            return None
        node = data.get("placement", {}).get("allocated_node")
        if not node:
            return None
        if different_from is not None and node == different_from:
            return None
        mapped = data.get("mapped_ports") or {}
        if mapped.get(required_port) is None:
            return None
        return data
    label = f"VM {vm_hash[:12]} dispatched + port {required_port}"
    if different_from is not None:
        label += f" (different from {different_from[:12]})"
    return poll(label, fetch, timeout=timeout)


@dataclass
class DispatchedVM:
    """A dispatched VM's reachable coordinates. Mutable: `refresh` re-reads
    placement because the mapped SSH port can change after start / restore."""
    hash: str
    crn_hash: str
    crn_host: str
    ssh_port: int

    def refresh(self, aleph_cli, timeout=300) -> "DispatchedVM":
        data = wait_for_dispatched(aleph_cli, self.hash, timeout=timeout)
        self.crn_hash = data["placement"]["allocated_node"]
        self.crn_host = resolve_crn_host(aleph_cli, self.crn_hash)
        self.ssh_port = int(data["mapped_ports"]["22"])
        return self


def create_dispatched_instance(aleph_cli, rootfs_hash, public_key_path, name, *,
                               vcpus="1", memory="2GiB", disk_size="4GiB", timeout=300) -> DispatchedVM:
    """Create an instance, wait for the scheduler to dispatch it, and resolve
    its reachable CRN host + mapped SSH port. Does not wait for SSH."""
    result = aleph_cli(
        "instance", "create", name,
        "--image", rootfs_hash,
        "--vcpus", vcpus,
        "--memory", memory,
        "--disk-size", disk_size,
        "--ssh-pubkey-file", public_key_path,
        "--chain", "eth",
        parse_json=True,
    )
    vm_hash = result["item_hash"]
    assert vm_hash, "Instance create should return an item_hash"
    data = wait_for_dispatched(aleph_cli, vm_hash, timeout=timeout)
    crn_hash = data["placement"]["allocated_node"]
    return DispatchedVM(
        hash=vm_hash,
        crn_hash=crn_hash,
        crn_host=resolve_crn_host(aleph_cli, crn_hash),
        ssh_port=int(data["mapped_ports"]["22"]),
    )
```

- [ ] **Step 2: Syntax check** — Run: `python -m py_compile tests/vm_helpers.py` — Expected: no output, exit 0.
- [ ] **Step 3: Commit**

```bash
git add tests/vm_helpers.py
git commit -m "test: extract shared VM helpers into tests/vm_helpers.py"
```

---

### Task 2: `rootfs_hash` session fixture + `aleph_cli` timeout support

**Files:**
- Modify: `tests/conftest.py`

- [ ] **Step 1: Add `timeout` support to the `aleph_cli` `run()` closure.** The `instance logs` command streams and never exits, so callers need a timeout that returns the partial output instead of hanging. Replace the `subprocess.run(...)` call inside `run()` (currently the single line `result = subprocess.run(cmd, capture_output=True, text=True, env=env)`) and update the signature:

```python
    def run(*args: str, parse_json: bool = False, check: bool = True, timeout: float | None = None) -> subprocess.CompletedProcess | dict | list | None:
        cmd = ["aleph", "--ccn", ccn_url, "--network", TESTNET_NETWORK]
        if parse_json:
            cmd.append("--json")
        cmd.extend(args)
        # Signing key + isolated CLI config (for scheduler + network-tag resolution).
        env = {
            **os.environ,
            "ALEPH_PRIVATE_KEY": private_key,
            "XDG_CONFIG_HOME": aleph_cli_config,
        }
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, env=env, timeout=timeout)
        except subprocess.TimeoutExpired as e:
            # Streaming commands (e.g. `instance logs`) never exit on their own;
            # return whatever was captured so far.
            return subprocess.CompletedProcess(
                cmd, returncode=None, stdout=e.stdout or "", stderr=e.stderr or "",
            )
        if check and result.returncode != 0:
            pytest.fail(
                f"CLI command failed: {' '.join(cmd)}\n"
                f"Exit code: {result.returncode}\n"
                f"Stdout: {result.stdout}\n"
                f"Stderr: {result.stderr}"
            )
        if parse_json:
            return json.loads(result.stdout) if result.stdout.strip() else None
        return result
```

- [ ] **Step 2: Add the `rootfs_hash` session fixture.** Place it just after the `rootfs_image` fixture:

```python
@pytest.fixture(scope="session")
def rootfs_hash(aleph_cli, rootfs_image) -> str:
    """Upload the rootfs image once per session; return its item_hash.

    Instance tests reuse this instead of each re-uploading the multi-hundred-MB
    image. Under xdist each worker has its own session, so the upload happens
    once per worker (idempotent — content-addressed)."""
    result = aleph_cli(
        "file", "upload", rootfs_image,
        "--storage-engine", "storage", "--chain", "eth",
        parse_json=True,
    )
    item_hash = result["item_hash"]
    assert item_hash, "Upload should return an item_hash"
    return item_hash
```

- [ ] **Step 3: Syntax check** — Run: `python -m py_compile tests/conftest.py` — Expected: exit 0.
- [ ] **Step 4: Commit**

```bash
git add tests/conftest.py
git commit -m "test: add rootfs_hash fixture and aleph_cli timeout support"
```

---

### Task 3: Refactor `tests/test_instances.py` onto shared helpers

**Files:**
- Modify: `tests/test_instances.py` (full rewrite — it currently inlines the helpers)

- [ ] **Step 1: Replace the file contents**

```python
import pytest

from tests.vm_helpers import create_dispatched_instance, wait_for_ssh


@pytest.mark.timeout(420)
def test_instance_create_and_ssh(aleph_cli, rootfs_hash, ssh_key_pair):
    """End-to-end: create instance → scheduler dispatches → CRN boots → SSH in.

    Discovery + SSH transport are handled by the shared helpers (CRN IPv4 +
    host-side mapped SSH port from `instance show --verbose`).
    """
    private_key_path, public_key_path = ssh_key_pair
    vm = create_dispatched_instance(
        aleph_cli, rootfs_hash, public_key_path, "test-instance",
    )
    output = wait_for_ssh(private_key_path, vm.crn_host, vm.ssh_port, timeout=60)
    assert output, "SSH round-trip should succeed once the VM is up"
```

- [ ] **Step 2: Syntax check** — Run: `python -m py_compile tests/test_instances.py` — Expected: exit 0.
- [ ] **Step 3: Commit**

```bash
git add tests/test_instances.py
git commit -m "test: refactor test_instances onto shared VM helpers"
```

---

### Task 4: Refactor `tests/test_migration.py` onto shared helpers

**Files:**
- Modify: `tests/test_migration.py` (full rewrite — currently inlines the helpers)

- [ ] **Step 1: Replace the file contents**

```python
"""Integration test for VM migration between CRNs.

Flow: create instance → SSH on initial CRN + write a marker file → unlink that
CRN → wait for scheduler to reallocate → SSH on new CRN + verify the marker
survived (disk state preserved across migration).

Requires two CRNs provisioned and linked (CRN_COUNT=2), scheduler dispatch
enabled, and an Ubuntu rootfs image (ALEPH_TESTNET_ROOTFS).
"""
import uuid

import pytest

from tests.vm_helpers import (
    create_dispatched_instance,
    resolve_crn_host,
    ssh_run,
    wait_for_dispatched,
    wait_for_ssh,
)


@pytest.mark.timeout(900)
def test_instance_migration(aleph_cli, rootfs_hash, ssh_key_pair, crn_nodes):
    """End-to-end: create → SSH → unlink CRN → scheduler migrates → SSH new CRN."""
    private_key_path, public_key_path = ssh_key_pair

    # --- Phase 1: Create instance, verify on initial CRN ---
    vm = create_dispatched_instance(
        aleph_cli, rootfs_hash, public_key_path, "migration-instance",
    )
    wait_for_ssh(private_key_path, vm.crn_host, vm.ssh_port, timeout=60)

    # Persist a marker we'll verify after migration. `sync` flushes the page
    # cache before the VM is stopped by the unlink so the marker hits disk.
    marker = uuid.uuid4().hex
    ssh_run(
        private_key_path, vm.crn_host, vm.ssh_port,
        f"echo {marker} > /root/migration-marker.txt && sync",
    )

    # --- Phase 2: Unlink the initial CRN and verify migration ---
    aleph_cli("node", "unlink", "--crn", vm.crn_hash, "--chain", "eth")

    # Budget covers both the scheduler moving the allocation and the new CRN
    # booting the VM + mapping SSH.
    migrated = wait_for_dispatched(
        aleph_cli, vm.hash, timeout=540, different_from=vm.crn_hash,
    )
    new_crn_hash = migrated["placement"]["allocated_node"]
    assert new_crn_hash != vm.crn_hash, (
        f"Scheduler should migrate to a different CRN, but got {new_crn_hash}"
    )
    new_host = resolve_crn_host(aleph_cli, new_crn_hash)
    new_port = int(migrated["mapped_ports"]["22"])

    wait_for_ssh(private_key_path, new_host, new_port, timeout=60)

    persisted = ssh_run(
        private_key_path, new_host, new_port,
        "cat /root/migration-marker.txt",
    ).strip()
    assert persisted == marker, (
        f"Migration should preserve disk state — expected marker {marker!r} "
        f"but got {persisted!r}"
    )
```

- [ ] **Step 2: Syntax check** — Run: `python -m py_compile tests/test_migration.py` — Expected: exit 0.
- [ ] **Step 3: Commit**

```bash
git add tests/test_migration.py
git commit -m "test: refactor test_migration onto shared VM helpers"
```

---

### Task 5: New `tests/test_instance_lifecycle.py` — fixture + logs test

**Files:**
- Create: `tests/test_instance_lifecycle.py`

- [ ] **Step 1: Write the module scaffold + logs test**

```python
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
```

- [ ] **Step 2: Syntax check** — Run: `python -m py_compile tests/test_instance_lifecycle.py` — Expected: exit 0.
- [ ] **Step 3: Commit**

```bash
git add tests/test_instance_lifecycle.py
git commit -m "test: add instance lifecycle module + logs test"
```

---

### Task 6: port-forward connectivity test

**Files:**
- Modify: `tests/test_instance_lifecycle.py`

- [ ] **Step 1: Append helpers + the test**

```python
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
```

- [ ] **Step 2: Syntax check** — Run: `python -m py_compile tests/test_instance_lifecycle.py` — Expected: exit 0.
- [ ] **Step 3: Commit**

```bash
git add tests/test_instance_lifecycle.py
git commit -m "test: add port-forward connectivity test"
```

---

### Task 7: reboot test

**Files:**
- Modify: `tests/test_instance_lifecycle.py`

- [ ] **Step 1: Append the test**

```python
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
```

- [ ] **Step 2: Syntax check** — Run: `python -m py_compile tests/test_instance_lifecycle.py` — Expected: exit 0.
- [ ] **Step 3: Commit**

```bash
git add tests/test_instance_lifecycle.py
git commit -m "test: add reboot test"
```

---

### Task 8: stop/start test

**Files:**
- Modify: `tests/test_instance_lifecycle.py`

- [ ] **Step 1: Append the test (runs last — most disruptive)**

```python
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
```

- [ ] **Step 2: Syntax check** — Run: `python -m py_compile tests/test_instance_lifecycle.py` — Expected: exit 0.
- [ ] **Step 3: Commit**

```bash
git add tests/test_instance_lifecycle.py
git commit -m "test: add stop/start test"
```

---

### Task 9: New `tests/test_instance_backup.py`

**Files:**
- Create: `tests/test_instance_backup.py`

- [ ] **Step 1: Write the module**

```python
"""Backup integration test: create → download → restore, validated with a
sentinel file. Uses its own VM because restore rewrites the rootfs.
"""
import uuid

import pytest

from tests.vm_helpers import create_dispatched_instance, ssh_run, wait_for_ssh


@pytest.fixture(scope="module")
def backup_vm(aleph_cli, rootfs_hash, ssh_key_pair):
    _, public_key_path = ssh_key_pair
    return create_dispatched_instance(
        aleph_cli, rootfs_hash, public_key_path, "backup-instance",
    )


@pytest.mark.timeout(1800)
def test_instance_backup_restore(backup_vm, aleph_cli, ssh_key_pair, tmp_path):
    """Back up a VM, change a sentinel file, restore, and confirm the sentinel
    reverted — proving create/download/restore round-trips on-disk state."""
    private_key_path, _ = ssh_key_pair
    wait_for_ssh(private_key_path, backup_vm.crn_host, backup_vm.ssh_port, timeout=120)

    original = uuid.uuid4().hex
    ssh_run(
        private_key_path, backup_vm.crn_host, backup_vm.ssh_port,
        f"echo {original} > /root/sentinel.txt && sync",
    )

    # Create the backup (blocks until complete) and download the archive.
    aleph_cli("instance", "backup", "create", backup_vm.hash, "--follow", "--chain", "eth")
    archive = tmp_path / "backup.tar"
    aleph_cli("instance", "backup", "download", backup_vm.hash, "-o", str(archive))
    assert archive.exists() and archive.stat().st_size > 0, "Backup archive should be non-empty"

    # Mutate the sentinel after the backup, then restore from the archive.
    modified = uuid.uuid4().hex
    ssh_run(
        private_key_path, backup_vm.crn_host, backup_vm.ssh_port,
        f"echo {modified} > /root/sentinel.txt && sync",
    )

    aleph_cli("instance", "backup", "restore", "--file", str(archive),
              backup_vm.hash, "--chain", "eth")

    # Restore restarts the VM; re-resolve placement and wait for SSH.
    backup_vm.refresh(aleph_cli, timeout=420)
    wait_for_ssh(private_key_path, backup_vm.crn_host, backup_vm.ssh_port, timeout=180)

    restored = ssh_run(
        private_key_path, backup_vm.crn_host, backup_vm.ssh_port,
        "cat /root/sentinel.txt",
    ).strip()
    assert restored == original, (
        f"Restore should revert the sentinel to {original!r}, got {restored!r}"
    )
```

- [ ] **Step 2: Syntax check** — Run: `python -m py_compile tests/test_instance_backup.py` — Expected: exit 0.
- [ ] **Step 3: Commit**

```bash
git add tests/test_instance_backup.py
git commit -m "test: add backup create/download/restore test"
```

---

### Task 10: Enable parallel runs (`pytest-xdist`)

**Files:**
- Modify: `pyproject.toml`
- Modify: `Justfile` (comment documenting the parallel invocation)

- [ ] **Step 1: Add the dependency.** In `pyproject.toml`, add `"pytest-xdist>=3.0"` to the `dependencies` list:

```toml
dependencies = [
    "pytest>=8.0",
    "pytest-timeout>=2.0",
    "pytest-xdist>=3.0",
    "pyyaml>=6.0",
]
```

- [ ] **Step 2: Document the parallel invocation.** In `Justfile`, update the `test` recipe's doc comment to mention parallelism:

```
# Run integration tests (extra args passed through, e.g. `just test -k test_credit`).
# For parallel runs across test modules: `just test -- -n auto --dist loadscope`
# (loadscope keeps a module's tests on one worker so each module's shared VM is
# created once; different modules run concurrently — a larger CRN hosts many VMs).
```

- [ ] **Step 3: Syntax check** — Run: `python -c "import tomllib; tomllib.load(open('pyproject.toml','rb'))"` — Expected: exit 0 (valid TOML).
- [ ] **Step 4: Commit**

```bash
git add pyproject.toml Justfile
git commit -m "test: add pytest-xdist for parallel module runs"
```

---

## Self-Review

**Spec coverage:**
- logs → Task 5 ✓
- port-forward (real connectivity) → Task 6 ✓
- reboot → Task 7 ✓
- stop/start → Task 8 ✓
- backup create/download/restore + sentinel → Task 9 ✓
- Extract + refactor existing files → Tasks 1, 3, 4 ✓
- `rootfs_hash` session fixture → Task 2 ✓
- Parallelism (pytest-xdist) → Task 10 ✓

**Type consistency:** `DispatchedVM(hash, crn_hash, crn_host, ssh_port)` defined in Task 1 and used consistently (`.crn_hash` in Task 4, `.refresh()` in Tasks 8 & 9, `.hash`/`.crn_host`/`.ssh_port` throughout). Helper names (`poll`, `ssh_run`, `ssh_ok`, `wait_for_ssh`, `wait_for_dispatched`, `resolve_crn_host`, `create_dispatched_instance`) match between definition (Task 1) and imports (Tasks 3–9). `aleph_cli(..., timeout=)` added in Task 2 and used in Task 5.

**Placeholder scan:** No TBD/TODO. Two tolerant-by-design spots are flagged for live tightening: `_forward_present` (unknown list JSON shape) and the custom-port `mapped_ports` lookup — both have explicit fallbacks noted in the spec's "confirm live" section.
