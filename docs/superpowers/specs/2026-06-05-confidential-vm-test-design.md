# Confidential VM (AMD SEV) test flow — design

Date: 2026-06-05
Status: approved (design review with Olivier)

## Goal

Add an end-to-end CI test of the confidential VM flow to guard against
regressions, using the aleph CLI's confidential commands:

1. Build a password-encrypted rootfs.
2. Upload it to the per-run test CCN.
3. Create a confidential instance referencing it.
4. Wait for the scheduler to place the instance.
5. Run the init sequence: `aleph instance confidential init-session`, then
   `aleph instance confidential start`.
6. SSH into the VM and verify it runs inside a TEE on an encrypted rootfs.

A single, statically provisioned AMD SEV server (the "TEE server") acts as the
confidential-capable CRN. It is **not** created or destroyed by CI; it persists
between runs. Its address and SSH user are GitHub Actions secrets
`AMD_SEV_SNP_HOST` and `AMD_SEV_SNP_USER` (plain Actions secrets, not part of
the `digitalocean` environment). CI authenticates with the existing
`DIGITALOCEAN_SSH_PRIVATE_KEY`.

## Architecture

Each PR run keeps its current shape — fresh CCN droplet + 2 DigitalOcean CRN
droplets — and gains the TEE server as a **third CRN (index 2)**:

```
GitHub runner
  ├─ creates CCN + CRN0 + CRN1 droplets (unchanged)
  ├─ writes .local/crn/2/ state for the TEE server (host/user from secrets,
  │    markers: static, confidential)
  └─ ssh → CCN droplet: crn-up.sh --install / --register with CRN_COUNT=3,
       then pytest (incl. new tests/test_confidential.py)
```

- The scheduler only places `requires_confidential` instances on CRNs that
  advertise `ENABLE_CONFIDENTIAL_COMPUTING`, so the confidential instance can
  only land on the TEE server. Regular instances keep using the DO CRNs (the
  TEE server may also receive regular instances — harmless).
- The `aleph instance create --confidential` / `instance confidential` command
  tree landed in aleph-cli **0.11.1**; the manifesto must pin at least that
  version.

### Locking the single TEE server

The `integration-tests` job gets a job-level concurrency group
(`amd-sev-snp-server`, `cancel-in-progress: false`) so only one run touches
the TEE server at a time. The existing workflow-level per-PR group (with
cancel-in-progress) stays.

Known caveat, accepted: GitHub keeps only one *pending* run per concurrency
group — if three runs queue, the middle one is cancelled. Fine at current
traffic; revisit (on-server lock file) if it bites. The group does not protect
against manual/local use of the server.

## `crn-up.sh`: static CRN support

Three new optional state files in `.local/crn/<idx>/`:

- **`static`** — this CRN is not ours to create/destroy.
  - `--provision` and `--destroy` skip it.
  - `--install` skips `apt upgrade`/base-package installation (one-time manual
    provisioning owns those) but still: writes `supervisor.env`, (re)starts
    vm-connector, installs the manifesto-pinned aleph-vm `.deb`, restarts the
    supervisor.
  - Before installing, `--install` **resets** the server: stop the supervisor,
    kill leftover qemu processes, wipe the aleph-vm execution root. Leftovers
    from a previous run must not leak in. The same reset runs in job teardown
    (`if: always()`), so a broken run cannot poison the next one.
- **`confidential`** — `supervisor.env` additionally gets
  `ALEPH_VM_ENABLE_CONFIDENTIAL_COMPUTING=true` and
  `ALEPH_VM_SEV_CTL_PATH=/opt/sevctl` (the aleph-vm default path).
- **`ssh-user`** — SSH user for this CRN (defaults to `root`).
  `ssh_crn`/`scp_to_crn` read it; when non-root, remote commands run through
  `sudo -n`.

`--register` needs no changes: the TEE server registers like any CRN against
the per-run CCN, and the registration dies with the CCN droplet, so there is
no unregister step.

## Encrypted rootfs: build & cache on the TEE server

The two upstream build scripts from aleph-vm
`examples/example_confidential_image/` (`build_debian_image.sh`,
`setup_debian_rootfs.sh`) are **vendored** into this repo under
`scripts/confidential-image/`, each with a header comment recording the
upstream source and the aleph-vm version they were copied from. Syncing them
with upstream is a conscious, reviewable act; the trade-off is that they no
longer auto-track the aleph-vm version under test — if upstream changes the
image/OVMF contract, our copies need a manual sync.

A new `scripts/confidential-rootfs.sh`, run from the CCN droplet, builds on
the TEE server over SSH (the server is static, has the disk, and the build
needs `guestmount`/`parted`/`cryptsetup` + root):

1. Compute a **cache key** = sha256 of (vendored build scripts + base image
   URL + test password).
2. Ensure the cache directory exists on the TEE server
   (`mkdir -p /opt/aleph-ci-cache`).
3. If `/opt/aleph-ci-cache/rootfs-<key>.img` exists → done.
4. Otherwise build on the TEE server: download the Debian 12 genericcloud
   qcow2, extract the rootfs via guestmount, scp the vendored scripts over and
   run `build_debian_image.sh --password <test password>`, move the result
   into the cache, prune older cache entries.
5. `scp` the cached image from the TEE server to the CCN droplet, where pytest
   uploads it via `aleph file upload`.

The password is a fixed, non-secret test constant — it protects nothing real.
It is exposed as `ALEPH_TESTNET_CONFIDENTIAL_PASSWORD` (with the same default
in `confidential-rootfs.sh` and `conftest.py`, so the image and the test
always agree).

First run after a build-script change rebuilds (~5–10 min); subsequent runs
hit the cache.

## Workflow changes (`pr-tests.yml`)

- Job-level `concurrency: { group: amd-sev-snp-server, cancel-in-progress: false }`
  on `integration-tests`.
- `CRN_COUNT=3` for install/register/log-collection loops; droplet
  creation/destroy loops use a separate `DO_CRN_COUNT=2`.
- New step **Configure TEE CRN state**: `ssh-keyscan` the TEE host, write
  `.local/crn/2/{droplet-ip,ssh-user,static,confidential}` on the CCN droplet
  from the `AMD_SEV_SNP_HOST`/`AMD_SEV_SNP_USER` secrets.
- New step **Build/fetch encrypted rootfs**: runs `confidential-rootfs.sh`,
  ending with the image at a known path on the CCN droplet; exported to pytest
  as `ALEPH_TESTNET_CONFIDENTIAL_ROOTFS`.
- **sevctl on the CCN droplet**: `confidential init-session` shells out to
  `sevctl` client-side. The aleph-vm `.deb` ships sevctl at `/opt/sevctl`
  (built from virtee/sevctl v0.6.0), so after `crn-up.sh --install` the CCN
  setup scps `/opt/sevctl` from the TEE server onto the CCN droplet's PATH —
  no separate download, and the client/CRN sevctl versions always match.
- **Firmware blob**: the OVMF firmware is *not* in the `.deb` — the CRN
  downloads it at VM-setup time by the item hash in the instance message
  (`environment.trusted_execution.firmware`), resolved against the test CCN.
  CI therefore downloads the canonical confidential OVMF blob once (from
  aleph.cloud, by its well-known default item hash), caches it in
  `/opt/aleph-ci-cache` on the TEE server, and scps it to the CCN droplet.
  Exposed to pytest as `ALEPH_TESTNET_CONFIDENTIAL_FIRMWARE`. The test
  uploads it to the test CCN and references the uploaded hash at create time
  (see below) — the default `vm-images`-aggregate resolution can't work on a
  fresh testnet CCN.
- **TEE host for placement assertion**: the workflow exports the TEE server
  address as `ALEPH_TESTNET_CONFIDENTIAL_CRN_HOST` so the test can assert the
  scheduler placed the instance on it.
- Teardown: existing droplet-destroy steps unchanged; an `if: always()` step
  re-runs the TEE reset. TEE journal/logs join the existing log-collection
  loop (using the per-CRN ssh-user).

## Test design (`tests/test_confidential.py`)

New session fixtures in `conftest.py`, mirroring `rootfs_image`:

- `confidential_rootfs` — path from `ALEPH_TESTNET_CONFIDENTIAL_ROOTFS`;
  skip when unset (keeps local runs working).
- `confidential_firmware` — path from `ALEPH_TESTNET_CONFIDENTIAL_FIRMWARE`;
  skip when unset.
- Password from `ALEPH_TESTNET_CONFIDENTIAL_PASSWORD` (shared default).

`test_confidential_instance_create_and_ssh` (timeout ~600 s, mirrors
`test_instance_create_and_ssh`):

1. **Upload**: `aleph file upload <img> --storage-engine storage --chain eth`
   → rootfs `item_hash`; same for the OVMF blob → firmware `item_hash`.
2. **Create**: `aleph instance create test-confidential --image <hash>
   --confidential --confidential-firmware <firmware-hash> --vcpus 1
   --memory 4GiB --disk-size 4GiB --ssh-pubkey-file <pub> --chain eth`.
   `--confidential-firmware` must be an explicit hash: the CLI's default
   resolves `defaults.firmware` from the `vm-images` aggregate, which does
   not exist on a fresh testnet CCN.
3. **Wait for scheduler**: poll `aleph instance show <hash> --verbose` until
   `placement.allocated_node` is set; resolve the CRN host via the
   corechannel aggregate (reuse `_resolve_crn_host`) and assert it equals
   `ALEPH_TESTNET_CONFIDENTIAL_CRN_HOST` (the TEE server).
4. **Init session**: `aleph instance confidential init-session <hash>
   --keep-session` (non-interactive; `--keep-session` makes reruns idempotent
   instead of hitting the overwrite prompt). Fetches the platform cert,
   verifies the chain against AMD roots via `sevctl`, posts session keys to
   the CRN.
5. **Start**: poll `aleph instance confidential start <hash>
   --secret <password> --firmware-file <ovmf> --json`. Polling because the VM
   must reach measurement-ready and the standalone `start` does not poll —
   the test retries on "not ready" failures. Validates the launch measurement
   and injects the disk-decryption secret.
6. **SSH**: poll `instance show --verbose` for `mapped_ports["22"]`, then SSH
   via the CRN IPv4 + mapped port (existing pattern; the CLI's own
   `instance ssh` uses the VM's IPv6, unreachable in this CI).
7. **Verify TEE from inside the guest** — the point of the test:
   - `dmesg | grep 'Memory Encryption Features active'` contains `SEV`
     (kernel confirms memory-encrypted execution);
   - the root filesystem sits on a dm-crypt device (`lsblk` shows a `crypt`
     ancestor of `/`) — proves the encrypted-rootfs path engaged.
8. **Teardown** (fixture finalizer, runs even on failure):
   `aleph instance delete <hash> -y` — `-y` is mandatory; without it the CLI
   silently aborts on non-TTY.

## Error handling

- All waits go through the existing `_poll` helper with explicit phase
  descriptions, so timeouts name the phase that hung (scheduling vs
  measurement vs SSH).
- `init-session`/`start` failures surface CLI stderr verbatim via the
  `aleph_cli` fixture's failure formatting.
- On any failure, job teardown still resets the TEE server.
- Known infra flake: SSH kex resets on freshly-booted VMs — tolerated by
  retrying inside `_poll`.

## One-time TEE server provisioning (`docs/tee-server.md`)

Checklist documenting what the server must have before CI can use it:

- BIOS: SEV + SEV-ES enabled, sufficient SEV ASIDs.
- Kernel: KVM AMD SEV active (`/sys/module/kvm_amd/parameters/sev` = `Y`).
- `docker.io`, `apparmor-profiles`, plus image-build deps: `guestmount`,
  `parted`, `cryptsetup`.
- (`sevctl` is *not* a manual prerequisite: the aleph-vm `.deb` installs it
  at `/opt/sevctl`.)
- CI SSH key accepted for `$AMD_SEV_SNP_USER`; passwordless sudo if non-root.
- A warning that CI rewrites supervisor.env, reinstalls the aleph-vm .deb and
  wipes the execution root on every run — do not use the box for anything
  stateful. (The rootfs cache at `/opt/aleph-ci-cache` is created by CI
  itself.)

## Out of scope

- SEV-SNP attestation reports (the current aleph-vm flow is SEV/SEV-ES launch
  measurement + secret injection; the server name notwithstanding).
- Confidential GPU instances.
- Multiple TEE servers / lock-file based locking.
- nightly.yml (currently disabled wholesale).
