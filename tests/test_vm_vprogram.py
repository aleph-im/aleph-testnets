"""V-PROGRAM end-to-end scenario: publish, schedule, boot on a real SNP host.

Chain under test:
  publish (python SDK; temporary until an aleph-rs rc ships vprogram support)
  -> CCN accepts the credit-paid message (pyaleph 0.10.3-rc6, phase 1)
  -> scheduler (aleph-vm-scheduler #193) plans it onto the SNP-capable CRN
     via TEE vcpu-model matching
  -> signed allocation POST to the CRN
  -> the CRN fetches the runtime manifest + bundle, extracts the measured
     platform image, downloads the fib-service workload volume the message
     references (content.workload), verity-verifies + mounts it, and boots an
     SEV-SNP guest that runs fib.
  -> a client attests the workload-bound measurement and calls GET /fib/10
     over the attested channel, getting 55.

The workload is delivered via the real mechanism (aleph.exec/1): fib-service
is a dm-verity ext4 volume the message points at, measured into the launch
via workload_roothash in the cmdline. The harness uploads the platform bundle
+ the workload volume + hash tree as STOREs and computes the per-workload
measurement (sev-snp-measure) so verification.measurements[].digest matches
what the guest produces.

Like the rest of the suite, this runs ON the CCN droplet (scheduler-api is
loopback-bound there).
"""

import asyncio
import hashlib
import json
import os
import time
import urllib.request

import pytest

# Reuse the SNP host SSH plumbing (host/user env gates, sudo -n elevation).
# The tests directory is not importable under every pytest invocation mode
# (e.g. `python -m pytest` from the repo root), so pin it onto sys.path.
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from conftest import _upload_with_balance_retry  # noqa: E402
from test_vm_snp import SNP_AMD_PRODUCT, SNP_ATTEST_CLI, SNP_HOST, _snp_run  # noqa: E402
from vm_helpers import resolve_crn_host  # noqa: E402

import re  # noqa: E402
import subprocess  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent

SNP_MEASUREMENT = os.environ.get("ALEPH_TESTNET_SNP_MEASUREMENT", "")
# The measured image + attest-cli staged by build-image/snp-artifacts.sh.
# .local/snp/bundle.tar.gz is the exact tarball CI fetched (sha256 ==
# SNP_IMAGE_HASH); .local/snp/image/ is its extracted contents.
SNP_STAGE_DIR = REPO_ROOT / ".local" / "snp"
BUNDLE_TARBALL = SNP_STAGE_DIR / "bundle.tar.gz"
BUNDLE_IMAGE_DIR = SNP_STAGE_DIR / "image"
# The user workload volume (fib-service), staged from the same image tarball.
WORKLOAD_DIR = SNP_STAGE_DIR / "workload"

# The exec-runtime cmdline template: byte-identical to what the daemon emits
# and what the guest measures (both roothashes, single spaces).
CMDLINE_TEMPLATE_EXEC = "console=ttyS0 root=/dev/mapper/verity-root ro roothash={platform_roothash} workload_roothash={workload_roothash}"


def _compute_workload_measurement(
    platform_roothash: str, workload_roothash: str
) -> str:
    """The per-workload SEV-SNP launch digest the publisher pins. Mirrors
    aleph-vm's compute_snp_measurement (sev-snp-measure 0.0.12): a function of
    OVMF + kernel + initrd + the exec cmdline (both roothashes) + vcpus + type.
    The image is measured for EPYC-v4 / 2 vCPU (nix flake default)."""
    from sevsnpmeasure import guest, vcpu_types, vmm_types
    from sevsnpmeasure.sev_mode import SevMode

    cmdline = CMDLINE_TEMPLATE_EXEC.format(
        platform_roothash=platform_roothash, workload_roothash=workload_roothash
    )
    digest = guest.calc_launch_digest(
        SevMode.SEV_SNP,
        2,
        vcpu_types.CPU_SIGS["EPYC-v4"],
        str(BUNDLE_IMAGE_DIR / "OVMF.fd"),
        str(BUNDLE_IMAGE_DIR / "bzImage"),
        str(BUNDLE_IMAGE_DIR / "initrd"),
        cmdline,
        0x1,  # guest_features: the sev-snp-measure CLI default the nix flake relies on
        vmm_type=vmm_types.VMMType.QEMU,
    )
    return digest.hex()


pytestmark = pytest.mark.skipif(
    not (SNP_HOST and SNP_MEASUREMENT),
    reason="SNP host or measurement not configured (ALEPH_TESTNET_CONFIDENTIAL_CRN_HOST / ALEPH_TESTNET_SNP_MEASUREMENT)",
)


def _http_json(url: str, timeout: float = 15) -> dict:
    with urllib.request.urlopen(url, timeout=timeout) as response:
        return json.load(response)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def poll(what: str, fn, timeout: float, interval: float = 5):
    """Poll fn() until it returns a truthy value; fail the test on timeout."""
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        last = fn()
        if last:
            return last
        time.sleep(interval)
    pytest.fail(f"Timed out after {timeout}s waiting for {what} (last: {last!r})")


@pytest.fixture(scope="session")
def tee_crn_registered(ccn_url) -> None:
    """Register the TEE CRN in the corechannel aggregate, scoped to its index.

    Deliberately done HERE and not in the workflow: a registered CRN is
    schedulable for instances too, and the upgrade tests must not have their
    instances placed on a static host that refuses root SSH and cannot be
    upgraded. This suite runs after them (alphabetical order), so the TEE CRN
    joins the pool only for the v-program scenario. Idempotent across runs.
    """
    tee_idx = os.environ.get("TEE_IDX", "1")
    env = {
        **os.environ,
        "CCN_URL": ccn_url,
        "SSH_KEY_FILE": os.environ.get("SSH_KEY_FILE", "/root/.ssh/id_ed25519"),
        "CRN_COUNT": str(int(tee_idx) + 1),
        "CRN_ONLY_INDEX": tee_idx,
    }
    result = subprocess.run(
        ["bash", str(REPO_ROOT / "scripts" / "crn-up.sh"), "--register"],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=600,
    )
    if result.returncode != 0:
        pytest.fail(
            f"TEE CRN registration failed:\nstdout: {result.stdout[-1500:]}\nstderr: {result.stderr[-1500:]}"
        )
    # Registration restarts the CRN supervisor (node-hash env update); its
    # usage endpoint 500s during the window. Publishing while the node looks
    # unhealthy parks the v-program, and the scheduler does not replan on
    # node recovery alone (observed run 30904164544), so wait until the node
    # answers before letting the test publish.
    poll(
        "TEE CRN usage endpoint healthy after registration",
        lambda: _http_json(f"http://{SNP_HOST}:4020/about/usage/system"),
        timeout=180,
    )
    # One extra node-watcher cycle so the scheduler's snapshot is refreshed
    # from the healthy endpoint before the VM delta triggers the replan.
    time.sleep(35)
    print(f"[vprogram] TEE CRN registered and healthy (index {tee_idx})")


def _build_runtime_manifest(bundle_ref: str) -> dict:
    """A strict RuntimeManifest (aleph-vprogram-runtime v1) for the staged
    bundle. sha256/size/platform_roothash are read from the staged files so
    the manifest is self-consistent with whatever tarball we upload; the CRN
    verifies the downloaded bundle against them."""
    sha256 = _sha256(BUNDLE_TARBALL)
    size = BUNDLE_TARBALL.stat().st_size
    platform_roothash = (BUNDLE_IMAGE_DIR / "rootfs.ext4.roothash").read_text().strip()
    return {
        "format": "aleph-vprogram-runtime",
        "format_version": 1,
        "name": "aleph-snp-attest",
        "version": "testnet",
        "platform": "sev_snp",
        "bundle": {
            "ref": bundle_ref,
            "sha256": sha256,
            "size": size,
            "members": {
                "ovmf": "image/OVMF.fd",
                "kernel": "image/bzImage",
                "initrd": "image/initrd",
                "platform_rootfs": "image/rootfs.ext4",
                "platform_hash_tree": "image/rootfs.ext4.verity",
            },
        },
        "boot": {
            "method": "qemu-direct-kernel",
            "kernel_hashes": True,
            "cpu_models": ["EPYC-v4"],
            "platform_roothash": platform_roothash,
            "cmdline_template": CMDLINE_TEMPLATE_EXEC,
        },
        "attestation": [
            {
                "protocol": "aleph.ra-tls",
                "version": "1",
                "transport": {"type": "tcp", "port": 8443},
            }
        ],
        "workload": {"contract": "aleph.exec/1", "upstream_port": 8080},
        "source": {
            "repo": "https://github.com/aleph-im/aleph-vm",
            "rev": "testnet",
            "build": "nix build .#image",
        },
    }


@pytest.fixture(scope="session")
def staged_refs(aleph_cli) -> dict:
    """Stage the STOREs the v-program references onto the testnet CCN.

    - bundle:   the measured platform-image tarball, uploaded so the CRN
                launch path downloads it in-protocol (runtime.ref -> manifest
                -> bundle).
    - manifest: a strict exec-runtime RuntimeManifest pointing at the bundle.
    - workload/hash_tree: the REAL fib-service dm-verity workload volume
                (workload.ext4) and its hash tree, staged from the image
                tarball. The CRN's launch path downloads these as
                content.workload, attaches them as extra disks, and the guest
                verity-verifies + execs fib. workload_roothash (read from the
                staged sidecar) goes into the measured cmdline.
    - measurement: the per-workload launch digest, computed here so the
                message's verification.measurements[].digest matches what the
                guest produces.
    """
    assert BUNDLE_TARBALL.is_file(), f"staged bundle tarball missing: {BUNDLE_TARBALL}"
    assert (
        WORKLOAD_DIR / "workload.ext4"
    ).is_file(), f"staged workload volume missing: {WORKLOAD_DIR}"

    bundle_ref = _upload_with_balance_retry(
        aleph_cli, str(BUNDLE_TARBALL), "v-program bundle"
    )

    manifest_json = json.dumps(_build_runtime_manifest(bundle_ref))
    manifest_path = WORKLOAD_DIR.parent / "runtime-manifest.json"
    manifest_path.write_text(manifest_json)
    manifest_ref = _upload_with_balance_retry(
        aleph_cli, str(manifest_path), "v-program manifest"
    )

    workload_ref = _upload_with_balance_retry(
        aleph_cli, str(WORKLOAD_DIR / "workload.ext4"), "v-program workload"
    )
    hash_tree_ref = _upload_with_balance_retry(
        aleph_cli,
        str(WORKLOAD_DIR / "workload.ext4.verity"),
        "v-program workload hash tree",
    )

    platform_roothash = (BUNDLE_IMAGE_DIR / "rootfs.ext4.roothash").read_text().strip()
    workload_roothash = (WORKLOAD_DIR / "workload.ext4.roothash").read_text().strip()
    measurement = _compute_workload_measurement(platform_roothash, workload_roothash)

    refs = {
        "manifest": manifest_ref,
        "bundle": bundle_ref,
        "workload": workload_ref,
        "hash_tree": hash_tree_ref,
        "workload_roothash": workload_roothash,
        "measurement": measurement,
    }
    print(
        f"[vprogram] staged refs: { {k: v for k, v in refs.items() if k != 'measurement'} }"
    )
    print(f"[vprogram] computed workload measurement: {measurement}")
    return refs


def build_vprogram_content(address: str, refs: dict) -> dict:
    """The hello-world v-program content, schema-validated before publishing."""
    content = {
        "address": address,
        "time": time.time(),
        "allow_amend": False,
        "payment": {"type": "credit"},
        "environment": {"internet": True},
        # vcpus MUST match the measured bundle: the launch measurement is a
        # function of the vCPU count (2 for the pinned EPYC-v4 image), so a
        # mismatch would boot but fail attestation.
        "resources": {"vcpus": 2, "memory": 2048, "seconds": 30},
        "runtime": {
            "ref": refs["manifest"],
            "comment": "aleph-snp-attest exec runtime (fib-service workload)",
        },
        "workload": {
            "ref": refs["workload"],
            "hash_tree": refs["hash_tree"],
            "roothash": refs["workload_roothash"],
        },
        "verification": {
            "backend": "sev_snp",
            "policy": 196608,
            "measurements": [
                {
                    "platform": "sev_snp",
                    "digest": refs["measurement"],
                    "vcpu_type": "EPYC-v4",
                }
            ],
        },
    }
    # Fail fast on schema drift, and publish the model's CANONICAL dump:
    # the message validator requires the raw content dict to equal the parsed
    # model's model_dump(exclude_none=True), so model-added defaults (e.g.
    # volumes: []) must be present in what we submit.
    from aleph_message.models.execution.vprogram import VerifiableProgramContent

    return VerifiableProgramContent(**content).model_dump(
        mode="json", exclude_none=True
    )


def publish_vprogram(ccn_url: str, private_key: str, content: dict) -> str:
    """Publish a V-PROGRAM message and return its item hash.

    Single seam for the publishing mechanism: replace this function's body
    with an `aleph vprogram create` CLI call once the aleph-rs rc ships it.
    """
    from aleph.sdk.chains.ethereum import ETHAccount
    from aleph.sdk.client import AuthenticatedAlephHttpClient
    from aleph_message.models import MessageType

    async def _publish() -> str:
        account = ETHAccount(private_key)
        async with AuthenticatedAlephHttpClient(
            account=account, api_server=ccn_url
        ) as client:
            message, status, _response = await client.submit(
                content=content,
                message_type=MessageType.v_program,
                channel="ALEPH-TESTNET-VPROGRAM",
                sync=True,
            )
            print(f"[vprogram] published {message.item_hash} (status: {status})")
            return str(message.item_hash)

    return asyncio.run(_publish())


def forget_vprogram(ccn_url: str, private_key: str, item_hash: str) -> None:
    """Best-effort FORGET so the scheduler stops retrying the parked VM."""
    from aleph.sdk.chains.ethereum import ETHAccount
    from aleph.sdk.client import AuthenticatedAlephHttpClient

    async def _forget() -> None:
        account = ETHAccount(private_key)
        async with AuthenticatedAlephHttpClient(
            account=account, api_server=ccn_url
        ) as client:
            await client.forget(
                hashes=[item_hash],
                reason="testnet v-program scenario teardown",
                channel="ALEPH-TESTNET-VPROGRAM",
            )

    try:
        asyncio.run(_forget())
    except Exception as error:  # noqa: BLE001 - teardown must not mask the test
        print(f"[vprogram] teardown forget failed (non-fatal): {error}")


def _find_assignment(plan: dict, item_hash: str):
    """The (node_key, node_entry) whose v_programs bucket holds item_hash.

    Walks the plan defensively instead of pinning its exact shape: node
    identity is the key mapping to the dict that carries the bucket.
    """
    stack = [plan]
    while stack:
        node = stack.pop()
        if isinstance(node, dict):
            for key, value in node.items():
                if isinstance(value, dict):
                    bucket = value.get("v_programs")
                    if isinstance(bucket, list) and item_hash in bucket:
                        return {"node_key": key, "entry": value}
                stack.append(value)
        elif isinstance(node, list):
            stack.extend(node)
    return None


def test_snp_crn_advertises_vcpu_models(crn_ssh_key):
    """The SNP CRN must advertise the measured vcpu models (#1052 QMP probe);
    without this the scheduler can never place a v-program anywhere."""
    usage = poll(
        "tee.sev_snp vcpu advertising on the SNP CRN",
        lambda: _http_json(f"http://{SNP_HOST}:4020/about/usage/system"),
        timeout=120,
    )
    tee = (usage.get("properties") or {}).get("tee") or {}
    models = (tee.get("sev_snp") or {}).get("supported_vcpu_types") or []
    assert (
        "EPYC-v4" in models
    ), f"SNP CRN vcpu models: {models} (usage properties: {usage.get('properties')})"


def test_vprogram_scheduled_to_snp_crn(
    ccn_url,
    private_key,
    scheduler_api_url,
    crn_ssh_key,
    staged_refs,
    aleph_cli,
    tee_crn_registered,
):
    test_start = time.time()
    content = build_vprogram_content(_address_of(private_key), staged_refs)
    item_hash = publish_vprogram(ccn_url, private_key, content)

    try:
        # 1. The CCN accepted and processed it (sync=True already gated on
        #    processing; double-check it is visible and typed correctly).
        message = poll(
            "message visible on the CCN",
            lambda: _http_json(f"{ccn_url}/api/v0/messages/{item_hash}").get("message"),
            timeout=60,
        )
        assert message["type"] == "V-PROGRAM"

        # 2. The scheduler planned it, and onto the SNP CRN specifically
        #    (vcpu/TEE eligibility: the DO CRN advertises no tee block).
        assignment = poll(
            "scheduler plan assignment",
            lambda: _find_assignment(
                _http_json(f"{scheduler_api_url}/api/v0/plan"), item_hash
            ),
            timeout=300,
            interval=10,
        )
        node_host = resolve_crn_host(aleph_cli, assignment["node_key"])
        assert node_host == SNP_HOST, (
            f"v-program assigned to {assignment['node_key']} ({node_host}), "
            f"not the SNP CRN ({SNP_HOST}): {json.dumps(assignment)}"
        )

        # 3. The signed allocation reached the CRN, which fetched the runtime
        #    manifest + bundle, extracted the measured image, and booted an
        #    SEV-SNP guest. The v1 execution list is keyed by item hash and
        #    lists a VM only once it is RUNNING, so the hash appearing there is
        #    the boot signal.
        def vprogram_running():
            try:
                listing = _http_json(f"http://{SNP_HOST}:4020/about/executions/list")
            except Exception as error:  # noqa: BLE001 - transient during launch
                print(f"[vprogram] execution list poll: {error}")
                return None
            return item_hash if item_hash in listing else None

        poll(
            "v-program VM RUNNING on the SNP CRN",
            vprogram_running,
            timeout=600,
            interval=15,
        )

        # It booted as an SEV-SNP confidential guest, not accidentally plain:
        # the daemon stands up a per-tap DHCP server only on the SNP launch
        # path, and it is the guest's boot-time udhcpc that draws a lease. The
        # log also carries the guest IP we attest against.
        snp_boot = _snp_run(
            crn_ssh_key,
            SNP_HOST,
            "journalctl -u aleph-vm-supervisor.service --no-pager | "
            "grep 'started per-tap DHCP server for SNP VM' | tail -1 || true",
            timeout=60,
        )
        assert snp_boot.stdout.strip(), (
            "v-program is RUNNING but no SNP DHCP server was started — did it boot as a plain VM? "
            f"(journal grep empty)"
        )
        print(f"[vprogram] booted as SEV-SNP guest: {snp_boot.stdout.strip()[-200:]}")

        guest_ip_match = re.search(r'guest_ip="([0-9.]+)"', snp_boot.stdout)
        assert (
            guest_ip_match
        ), f"could not parse guest_ip from DHCP log: {snp_boot.stdout.strip()}"
        guest_ip = guest_ip_match.group(1)

        # 4. Attest the workload and run it. The message pinned the per-workload
        #    measurement (platform + fib workload roothash), so a successful
        #    attestation proves the exact fib volume was measured into the
        #    launch. Fetching /fib/10 over the attested channel (the attest-agent
        #    proxies :8443 -> the workload's :8080) proves fib actually runs.
        measurement = staged_refs["measurement"]
        fib_url = f"https://{guest_ip}:8443/fib/10"
        attest_cmd = (
            f"{SNP_ATTEST_CLI} attest --url {fib_url} "
            f"--expected-measurement {measurement} --amd-product {SNP_AMD_PRODUCT}"
        )

        def attest_ok():
            r = _snp_run(crn_ssh_key, SNP_HOST, attest_cmd, timeout=120)
            print(f"[vprogram] attest rc={r.returncode}\n{r.stdout}\n{r.stderr}")
            return r if r.returncode == 0 else None

        attest = poll(
            f"aleph-attest-cli attest against {fib_url} (workload-bound measurement)",
            attest_ok,
            timeout=420,
            interval=15,
        )
        assert (
            "Attestation valid: true" in attest.stdout
        ), f"attest-cli did not report a valid attestation:\n{attest.stdout}"
        assert (
            measurement in attest.stdout.replace(" ", "")
        ), f"attested measurement is not the pinned workload measurement:\n{attest.stdout}"
        # The attested response body is fib-service's JSON for /fib/10.
        assert (
            '"result":55' in attest.stdout.replace(" ", "")
        ), f"fib-service did not return fib(10)=55 over the attested channel:\n{attest.stdout}"
        print(f"[vprogram] fib(10)=55 verified over the attested channel at {fib_url}")
    finally:
        forget_vprogram(ccn_url, private_key, item_hash)


def _address_of(private_key: str) -> str:
    from aleph.sdk.chains.ethereum import ETHAccount

    return ETHAccount(private_key).get_address()
