"""End-to-end confidential VM (AMD SEV) test.

Flow: upload encrypted rootfs + OVMF firmware → create a confidential
instance → scheduler places it on the TEE server → `confidential
init-session` (cert chain verification + session keys via sevctl) →
`confidential start` (launch-measurement validation + disk secret
injection) → SSH in → verify SEV is active and the rootfs is dm-crypt.

Requires the static TEE server configured as a confidential CRN (see
docs/tee-server.md) and the artifacts prepared by
scripts/confidential-artifacts.sh. All `ALEPH_TESTNET_CONFIDENTIAL_*`
fixtures skip when unset, so local runs without a TEE server still pass.
"""
import json
import urllib.request

import pytest

from tests.vm_helpers import (
    NODESTATUS_ADDR,
    delete_instance,
    poll,
    resolve_crn_host,
    ssh_run,
    wait_for_dispatched,
    wait_for_ssh,
)


@pytest.mark.timeout(900)
def test_confidential_instance_create_and_ssh(
    aleph_cli,
    confidential_rootfs_hash,
    confidential_firmware_hash,
    confidential_firmware,
    confidential_password,
    confidential_crn_host,
    ssh_key_pair,
):
    private_key_path, public_key_path = ssh_key_pair

    # Fail fast if the TEE CRN is not linked in the corechannel aggregate
    # (e.g. unlinked by the migration test without re-linking) — placement
    # can never happen then, and this beats a 300 s poll timeout.
    nodes = aleph_cli(
        "node", "list", "--type", "crn", "--all",
        "--corechannel-address", NODESTATUS_ADDR,
        parse_json=True,
    ) or []
    assert any(confidential_crn_host in n.get("address", "") for n in nodes), (
        f"TEE CRN {confidential_crn_host} is not linked in the corechannel "
        "aggregate; confidential instances cannot be scheduled."
    )

    # Fail fast if the TEE CRN does not advertise confidential computing:
    # the scheduler reads computing.ENABLE_CONFIDENTIAL_COMPUTING from this
    # exact endpoint and will never place the instance without it.
    config_url = f"http://{confidential_crn_host}:4020/status/config"
    try:
        crn_config = json.loads(urllib.request.urlopen(config_url, timeout=10).read())
    except Exception as e:
        pytest.fail(f"Cannot fetch TEE CRN config at {config_url}: {e}")
    computing = crn_config.get("computing") or {}
    assert computing.get("ENABLE_CONFIDENTIAL_COMPUTING") is True, (
        f"TEE CRN does not advertise confidential computing "
        f"(computing section: {computing!r}); check supervisor.env and that "
        "the host supports SEV/SEV-ES."
    )

    result = aleph_cli(
        "instance", "create", "test-confidential",
        "--image", confidential_rootfs_hash,
        "--confidential",
        "--confidential-firmware", confidential_firmware_hash,
        "--vcpus", "1",
        "--memory", "4GiB",
        "--disk-size", "4GiB",
        "--ssh-pubkey-file", public_key_path,
        "--chain", "eth",
        parse_json=True,
    )
    vm_hash = result["item_hash"]
    assert vm_hash, "Instance create should return an item_hash"

    try:
        # Scheduler placement. required_port=None: a confidential VM is
        # placed before it starts; ports map only after secret injection.
        data = wait_for_dispatched(aleph_cli, vm_hash, timeout=300, required_port=None)
        crn_hash = data["placement"]["allocated_node"]
        crn_host = resolve_crn_host(aleph_cli, crn_hash)
        assert crn_host == confidential_crn_host, (
            f"Confidential instance landed on {crn_host}, expected the TEE "
            f"server {confidential_crn_host}"
        )

        # Init sequence: fetch + verify the platform cert against AMD roots,
        # derive session keys (sevctl), post them to the CRN.
        aleph_cli("instance", "confidential", "init-session", vm_hash, "--keep-session")

        # Start: validate the launch measurement against the known firmware
        # and inject the disk-decryption secret. The standalone `start` does
        # not poll for measurement-readiness, so retry until the CRN reports
        # the measurement (the VM needs a moment to boot to that point).
        def try_start():
            r = aleph_cli(
                "instance", "confidential", "start", vm_hash,
                "--secret", confidential_password,
                "--firmware-file", confidential_firmware,
                "--json",
                check=False,
            )
            if r.returncode == 0:
                return r
            raise RuntimeError(f"confidential start: {r.stderr.strip()[-500:]}")

        poll("Confidential start (measurement + secret injection)", try_start,
             timeout=300, interval=10)

        # The guest now decrypts its disk and boots; wait for SSH.
        data = wait_for_dispatched(aleph_cli, vm_hash, timeout=300)
        ssh_port = int(data["mapped_ports"]["22"])
        wait_for_ssh(private_key_path, crn_host, ssh_port, timeout=120)

        # TEE verification — the point of this test.
        # 1. The guest kernel reports SEV memory encryption active.
        dmesg = ssh_run(
            private_key_path, crn_host, ssh_port,
            "dmesg | grep -i 'Memory Encryption Features active' || true",
        )
        assert "SEV" in dmesg, f"Guest kernel does not report SEV active: {dmesg!r}"

        # 2. The root filesystem is on a dm-crypt mapping (the encrypted
        #    rootfs actually engaged, rather than some fallback plain disk).
        root_src = ssh_run(
            private_key_path, crn_host, ssh_port, "findmnt -no SOURCE /",
        ).strip()
        assert root_src.startswith("/dev/mapper/"), (
            f"Root filesystem is not dm-crypt mapped: {root_src!r}"
        )
    finally:
        delete_instance(aleph_cli, vm_hash)
