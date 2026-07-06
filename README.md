# aleph-testnets: aleph-vm upgrade checks branch

This branch (`od/aleph-vm-upgrade-checks`) is a stripped-down variant of
aleph-testnets dedicated to one thing: validating aleph-vm upgrades and the
adoption path of the Rust supervisor port, without a human in the loop. Every
other test has been removed; only the deploy machinery and the upgrade
scenarios in `tests/test_vm_upgrade.py` remain. Do not merge this branch into
`main`.

## Scenarios

- **Scenario A, release upgrade** (`test_release_upgrade_preserves_running_instance`):
  deploy a CRN with the latest aleph-vm release deb, create an instance with a
  port forward and a marker file, upgrade the CRN to a candidate deb built from
  a branch (CI artifact), and assert the instance survived the package upgrade
  (marker readable over SSH, port forwards mapped) and that lifecycle
  operations (stop/start) still work afterwards.

- **Scenario B, supervisor implementation swap**
  (`test_supervisor_impl_swap_preserves_running_instance`): on a CRN running
  the candidate deb with `ALEPH_VM_SUPERVISOR_IMPL=python`, create an instance
  with marker + port forward, flip the env var to `rust` in
  `/etc/aleph-vm/supervisor.env`, restart `aleph-vm-supervisor`, and assert the
  instance survived and the CRN's executions listing still reports it. Then
  swap back to `python` and assert lifecycle operations still work. Gated: it
  only runs with `UPGRADE_CHECK_RUST=1` (the Rust daemon must serve the
  read-only RPC surface, aleph-vm Rust port increment 2+).
  `UPGRADE_CHECK_RUST_LIFECYCLE=1` additionally exercises stop/start while the
  Rust daemon is active (increment 3+).

## Environment variables

| Variable | Default | Meaning |
| --- | --- | --- |
| `ALEPH_VM_UPGRADE_BRANCH` | `dev` | aleph-vm branch whose CI deb scenario A upgrades to |
| `UPGRADE_CHECK_RUST` | unset | `1` enables scenario B |
| `UPGRADE_CHECK_RUST_LIFECYCLE` | unset | `1` also runs stop/start under the Rust daemon in scenario B |
| `SSH_KEY_FILE` | `~/.ssh/id_ed25519` | key with root access to the CRN hosts (same convention as scripts/crn-up.sh) |

## Running

The suite assumes the standard aleph-testnets deployment (CCN stack + CRN
droplets, see `scripts/local-up.sh` and `scripts/crn-up.sh`) with one twist:
the CRN must initially be installed with the *baseline* release deb, e.g.

    ALEPH_VM_VERSION=1.13.0 CCN_URL=http://<ccn>:4024 ./scripts/crn-up.sh --install

(an explicit `ALEPH_VM_VERSION` wins over this branch's manifesto `branch:`
pin). The mid-test upgrade is performed by `scripts/crn-up.sh --upgrade`,
which the tests invoke themselves; it needs an authenticated `gh` CLI
(`GH_TOKEN`) to download the candidate deb from aleph-vm CI artifacts.

In CI, `.github/workflows/upgrade-checks.yml` wires all of this up and is
triggered manually (`workflow_dispatch`) with the baseline version and
candidate branch as inputs.

The manifesto pins `aleph-vm` to `branch: "dev"` on this branch; runs override
it (via `ALEPH_VM_UPGRADE_BRANCH` / the workflow input) to the increment
branch under test.
