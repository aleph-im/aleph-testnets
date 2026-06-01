# Instance Lifecycle Integration Tests — Design Spec

## Purpose

Extend the testnet integration suite with tests that exercise the post-creation
instance lifecycle through the `aleph instance` CLI subcommands. These guard the
compute path during an ongoing rewrite of `aleph-vm`, where many things are
expected to break unexpectedly; the goal is to *identify* regressions, not just
prove the happy path.

New capabilities covered:

1. **logs** — read console logs from a dispatched VM (`aleph instance logs`).
2. **port-forward** — create an IPv4 TCP port forward and verify real
   connectivity (`aleph instance port-forward create`).
3. **reboot** — reboot a VM and confirm it comes back (`aleph instance reboot`).
4. **stop / start** — stop a VM, confirm it is down, start it, confirm it is up
   (`aleph instance stop` / `aleph instance start`).
5. **backup** — back up a running VM, download the archive, and restore it,
   verifying a sentinel file is reverted (`aleph instance backup
   create / download / restore`).

For 3/4/5 the core validation is an SSH round-trip after the operation. Backup
additionally uses a sentinel file to prove the restore reverted on-disk changes.

## Context

- `tests/test_instances.py` already covers create → dispatch → SSH.
- `tests/test_migration.py` covers create → SSH → unlink CRN → migrate → SSH.
- Both duplicate a set of helpers: `_poll`, `_resolve_crn_host`, `_ssh_run`,
  `_wait_for_ssh`, `_wait_for_dispatched`, the `NODESTATUS_ADDR` constant, and
  the "create + wait for dispatch + resolve host/port + wait for SSH" bootstrap.
- SSH reaches the VM over the CRN's **IPv4 + host-side mapped port** (from
  `instance show --verbose` `mapped_ports`). The CLI's own `instance ssh` uses
  the VM's IPv6, which is not reachable in CI — so tests do not use it.
- Tests run via `scripts/local-up.sh --test`, which calls
  `pytest -v --junitxml=results.xml "$@"`; extra args pass straight through
  (e.g. `just test -- -k foo`).

## Design Decisions (settled during brainstorming)

- **VM sharing — hybrid.** Non-destructive ops share one VM; backup gets its
  own VM (restore rewrites the rootfs).
- **Refactor scope — extract and refactor the existing two files**, not just the
  new ones. All shared helpers move to one module; `test_instances.py` and
  `test_migration.py` are updated to consume it.
- **Port-forward — verify real connectivity**, not just config propagation.
- **Backup restore — re-upload the downloaded `.tar`** via `restore --file`
  (the CLI accepts the downloaded archive directly; no QCOW2 extraction needed).
- **Parallelism — designed in.** Tests are parallel-safe; modules run
  concurrently under `pytest-xdist`. A single larger CRN can host multiple VMs,
  so concurrency does **not** require one CRN per VM.

## Architecture

### New module: `tests/vm_helpers.py`

Holds the previously-duplicated helpers as public functions:

- `NODESTATUS_ADDR` — constant.
- `poll(description, fetch, timeout, interval=5)` — unchanged logic.
- `resolve_crn_host(aleph_cli, crn_hash) -> str`.
- `ssh_run(private_key_path, host, port, command, timeout=15) -> str` — one-shot
  SSH, raises on non-zero exit.
- `wait_for_ssh(private_key_path, host, port, timeout=60)`.
- `wait_for_dispatched(aleph_cli, vm_hash, timeout=300, *, different_from=None,
  required_port="22")` — polls `instance show --verbose` until allocated +
  the named mapped port is present. `required_port` generalises the existing
  hardcoded `"22"` so the port-forward test can wait on a custom port.
- A small `DispatchedVM` handle:

  ```python
  @dataclass
  class DispatchedVM:
      hash: str
      crn_host: str
      ssh_port: int

      def refresh(self, aleph_cli) -> "DispatchedVM":
          """Re-read `instance show` and update crn_host / ssh_port.
          The mapped SSH port can change after start / restore."""
  ```

- `create_dispatched_instance(aleph_cli, rootfs_hash, public_key_path, name,
  *, vcpus="1", memory="2GiB", disk_size="4GiB", timeout=300) -> DispatchedVM`
  — sends `instance create`, waits for dispatch, resolves host/port, returns a
  `DispatchedVM`. Does **not** wait for SSH (callers decide when).

### `tests/conftest.py` additions

- `rootfs_hash` (session-scoped) — uploads `rootfs_image` once via
  `aleph file upload ... --storage-engine storage --chain eth` and returns the
  `item_hash`. Replaces the per-test upload in every instance test, so the
  ~hundreds-of-MB rootfs is uploaded once per session.

### Refactor of existing files

- `test_instances.py`: delete its local helpers; import from `vm_helpers`; use
  the `rootfs_hash` fixture and `create_dispatched_instance`. Behaviour
  unchanged.
- `test_migration.py`: same — delete local helper copies, import from
  `vm_helpers`, use `rootfs_hash`. The migration-specific `different_from`
  polling is already covered by `wait_for_dispatched`.

### New file: `tests/test_instance_lifecycle.py`

A **module-scoped** `running_vm` fixture creates one VM (via
`create_dispatched_instance` + `wait_for_ssh`) shared by the tests below. Tests
are ordered least → most disruptive and each leaves the VM running:

1. `test_instance_logs(running_vm, aleph_cli)`
   - `instance logs` is a streaming command. Run it with a capture timeout
     (treat `TimeoutExpired` as expected and read partial stdout).
   - Assert non-empty output containing a recognisable boot/console marker
     (exact marker confirmed live; falls back to non-empty assertion).

2. `test_instance_port_forward(running_vm, aleph_cli)`
   - `port-forward create <hash> <PORT> --tcp true --chain eth`.
   - `port-forward refresh <hash> --chain eth` (CRN re-reads the aggregate).
   - Assert the forward appears in
     `port-forward list --vm-id <hash> --json`.
   - **Connectivity:** SSH in, start a one-shot listener on `<PORT>` serving a
     known token (e.g. a tiny HTTP responder via `python3`); poll
     `instance show --verbose` for the host-side mapped port for `<PORT>`
     (fall back to `port-forward list` if custom ports are not surfaced there);
     from the test host, connect to `crn_host:mapped_port` and assert the token
     comes back.
   - `try/finally`: `port-forward delete <hash> --port <PORT> --chain eth` and
     kill the in-VM listener.

3. `test_instance_reboot(running_vm, aleph_cli)`
   - Read `/proc/sys/kernel/random/boot_id` over SSH.
   - `instance reboot <hash> --chain eth`.
   - `wait_for_ssh`, then assert the boot_id changed — proving a real reboot,
     not merely continued reachability.

4. `test_instance_stop_start(running_vm, aleph_cli)` — runs last (most
   disruptive).
   - Confirm SSH works.
   - `instance stop <hash> --chain eth`; poll until SSH stops succeeding.
   - `instance start <hash> --chain eth`; `wait_for_dispatched` + `refresh`
     (mapped port may change) + `wait_for_ssh`; assert reachable again.

### New file: `tests/test_instance_backup.py`

Its own module-scoped VM. One test `test_instance_backup_restore`:

1. `wait_for_ssh`; SSH write `/root/sentinel.txt` = token `A` + `sync`.
2. `instance backup create <hash> --follow --chain eth` (waits for completion).
3. `instance backup download <hash> -o <tmp>/backup.tar`; assert the file exists
   and is non-empty.
4. SSH overwrite `/root/sentinel.txt` = token `B` + `sync`.
5. `instance backup restore --file <tmp>/backup.tar <hash> --chain eth`.
6. Wait for the VM to come back (re-`wait_for_dispatched` + `refresh` +
   `wait_for_ssh`, since restore restarts the VM).
7. Assert `cat /root/sentinel.txt` == `A` — the restore reverted the change.
8. `try/finally` best-effort `backup delete` cleanup.

## Parallelism

- Add `pytest-xdist` to `pyproject.toml` dependencies.
- Each new test file owns a module-scoped VM, so
  `pytest -n auto --dist loadscope` keeps a module's tests sequential on one
  worker while **different modules run concurrently** (lifecycle ‖ backup ‖ the
  existing single-test files).
- Within the lifecycle module the ops share one VM and therefore run in order on
  one worker — the accepted hybrid trade-off.
- Default `just test` stays serial; parallelism is opt-in
  (`just test -- -n auto --dist loadscope`).
- Capacity: concurrent modules mean concurrent VMs, but one suitably-sized CRN
  can host several VMs — so this does not require `CRN_COUNT` to scale with VM
  count. Requires at least the CRNs the existing suite already needs.

## Parallel-safety checklist

- VM identity is the message hash (unique per create) — no name collisions.
- Port-forward aggregate entries are keyed by VM hash — no cross-test overlap.
- All local files use `tmp_path` / `tmp_path_factory` — unique per test.
- The session CLI config dir is written once at setup, then read-only — safe for
  concurrent reads across workers.
- `rootfs_hash` is session-scoped; under xdist each worker has its own session,
  so the upload happens once per worker (idempotent — same content hash).

## Error handling

- Reuse `poll` with generous, explicit timeouts; failures call `pytest.fail`
  with the last error (matches existing style).
- `aleph_cli(..., check=True)` surfaces CLI errors with full stdout/stderr.
- `try/finally` cleanup for port-forward entries and in-VM listeners so a
  failure does not leak aggregate state into other runs.
- Per-test `@pytest.mark.timeout(...)`: lifecycle ops modest (shared VM created
  once); backup generous (create+download+restore can be minutes).

## To confirm live during implementation

These are verification points, not assumptions to hard-code blindly:

1. `instance logs` termination/format (streaming vs one-shot; whether `--json`
   changes framing) — picks the capture strategy and the content assertion.
2. Whether a custom forwarded port appears in `instance show --verbose`
   `mapped_ports`, or only via `port-forward list` — selects the lookup path.
3. `backup create --follow` completion semantics and whether `restore` restarts
   the VM (changes whether step 6 re-polls dispatch).
4. Whether `reboot` / `start` preserve the mapped SSH port (decides if `refresh`
   is strictly required) — `refresh` is cheap, so it is called regardless.

## Out of scope

- `instance erase`, `delete`, `price`, `update` of port forwards, UDP forwards,
  `--include-volumes` backups, `--volume-ref` restore.
- IPv6 reachability / the CLI's native `instance ssh`.
- CI workflow changes (`.github/workflows`): the new tests run under the
  existing `--test` path; enabling parallel flags in CI is a separate decision.

## Verification

- `python -m py_compile` / `pytest --collect-only` for import & syntax sanity
  (a full run needs a live testnet, which is the operator's environment).
- Re-run `test_instances.py` and `test_migration.py` after the refactor to
  confirm unchanged behaviour.
- Run the new tests against a live testnet with `CRN_COUNT >= 2` and a rootfs
  image, both serially and with `-n auto --dist loadscope`.
