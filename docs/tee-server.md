# TEE server (AMD SEV) — one-time provisioning

A single static server provides the confidential-computing CRN for the CI
test suite (`tests/test_confidential.py`). CI does **not** create or destroy
it; everything below is one-time manual setup. Its coordinates live in the
GitHub Actions secrets `AMD_SEV_SNP_HOST` and `AMD_SEV_SNP_USER` (plain
Actions secrets, not part of the `digitalocean` environment), and CI
authenticates with the same SSH key as the DigitalOcean droplets
(`DIGITALOCEAN_SSH_PRIVATE_KEY`).

## Requirements

- **BIOS/firmware**: AMD SEV and SEV-ES enabled, with sufficient SEV ASIDs.
- **Kernel**: KVM AMD SEV active — `cat /sys/module/kvm_amd/parameters/sev`
  must print `Y` (and `sev_es` likewise for SEV-ES).
- **OS**: any distro with a published aleph-vm `.deb` (debian-12, debian-13,
  ubuntu-22.04, ubuntu-24.04) — `crn-up.sh` detects the distro via
  `/etc/os-release` and installs the matching variant.
- **OS packages** (apt names; adjust to taste):
  `apt install docker.io apparmor-profiles sudo guestmount parted cryptsetup`
  - `docker.io`, `apparmor-profiles`: standard CRN dependencies.
  - `sudo`: required even for root logins — the vendored image build script
    invokes `sudo` internally.
  - `guestmount` (libguestfs-tools), `parted`, `cryptsetup`: encrypted
    rootfs build dependencies.
- **SSH access**: the CI key accepted for `$AMD_SEV_SNP_USER`. If that user
  is not `root`, it needs **passwordless sudo** (`NOPASSWD:ALL`).
- **Network**: reachable from the CI-created DigitalOcean droplets —
  inbound TCP 4020 (CRN supervisor) and the high ports aleph-vm maps for VM
  SSH forwarding. No NAT/firewall rules that would block them.
- `sevctl` is **not** a manual prerequisite: the aleph-vm `.deb` installs it
  at `/opt/sevctl` on every CI run.

## What CI does to this server on every run

Treat the box as disposable from the neck up — do not run anything stateful
on it:

- `scripts/tee-reset.sh`: stops `aleph-vm-supervisor`, kills leftover QEMU
  processes, wipes `/var/lib/aleph/vm/*` and `/var/cache/aleph/vm/*`
  (at install time and again in `if: always()` teardown).
- `scripts/crn-up.sh --install`: writes `/etc/aleph-vm/supervisor.env`
  (pointing at the run's fresh CCN, with
  `ALEPH_VM_ENABLE_CONFIDENTIAL_COMPUTING=true`), recreates the
  `vm-connector` container, installs the manifesto-pinned aleph-vm `.deb`,
  restarts the supervisor.
- `scripts/crn-up.sh --register`: registers it as a CRN in the run's
  corechannel aggregate (which dies with the run's CCN droplet).
- `scripts/confidential-artifacts.sh`: builds/caches the encrypted test
  rootfs under `/opt/aleph-ci-cache/` (created automatically; the only
  state that intentionally survives runs).

## Concurrency

The `integration-tests` job carries a `concurrency: amd-sev-snp-server`
group so only one CI run touches the server at a time. GitHub keeps a single
pending run per group; if more stack up, intermediate ones are cancelled.
Manual use of the server while CI runs is not protected against.
