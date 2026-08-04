"""V-PROGRAM end-to-end scenario, Milestone A: the control plane.

Chain under test:
  publish (python SDK; temporary until an aleph-rs rc ships vprogram support)
  -> CCN accepts the credit-paid message (pyaleph 0.10.3-rc6, phase 1)
  -> scheduler (aleph-vm-scheduler #193) plans it onto the SNP-capable CRN
     via TEE vcpu-model matching
  -> signed allocation POST to the CRN
  -> the CRN (aleph-vm #1052) fetches the message and reports the clean
     "does not implement the SEV-SNP launch path yet" error.

The final assertion is the not-implemented error ON PURPOSE: launch wiring is
a designated later aleph-vm increment. When it lands, flip the last section
of test_vprogram_scheduled_to_snp_crn to boot + attest-cli verification and
this file becomes the full hello-world E2E test.

Like the rest of the suite, this runs ON the CCN droplet (scheduler-api is
loopback-bound there).
"""

import asyncio
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
from test_vm_snp import SNP_HOST, _snp_run  # noqa: E402

# The pinned mainnet SNP runtime bundle (aleph-vm PR #1050 reference values).
# The measurement digest is read from the staged image at runtime
# (ALEPH_TESTNET_SNP_MEASUREMENT) so it always matches the pinned bundle.
RUNTIME_BUNDLE_REF = "87287e4a5c8d7554a50f982cd681b64b2600c0bbb1c0b1e618465e022e01b977"
# Milestone A placeholder: the aleph.builtin/1 runtime serves its built-in
# workload, but the message schema requires a workload block. pyaleph rc6
# performs no ref-existence checks for v-programs, so these placeholder
# hashes are accepted; they become real refs in the launch milestone.
PLACEHOLDER_WORKLOAD = {
    "ref": "beef" * 16,
    "hash_tree": "feed" * 16,
    "roothash": "cdcd" * 16,
}

SNP_MEASUREMENT = os.environ.get("ALEPH_TESTNET_SNP_MEASUREMENT", "")

pytestmark = pytest.mark.skipif(
    not (SNP_HOST and SNP_MEASUREMENT),
    reason="SNP host or measurement not configured (ALEPH_TESTNET_CONFIDENTIAL_CRN_HOST / ALEPH_TESTNET_SNP_MEASUREMENT)",
)


def _http_json(url: str, timeout: float = 15) -> dict:
    with urllib.request.urlopen(url, timeout=timeout) as response:
        return json.load(response)


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


def build_vprogram_content(address: str) -> dict:
    """The hello-world v-program content, schema-validated before publishing."""
    content = {
        "address": address,
        "time": time.time(),
        "allow_amend": False,
        "payment": {"type": "credit"},
        "environment": {"internet": True},
        "resources": {"vcpus": 1, "memory": 2048, "seconds": 30},
        "runtime": {
            "ref": RUNTIME_BUNDLE_REF,
            "comment": "aleph-snp-attest measured bundle (aleph.builtin/1)",
        },
        "workload": PLACEHOLDER_WORKLOAD,
        "verification": {
            "backend": "sev_snp",
            "policy": 196608,
            "measurements": [
                {
                    "platform": "sev_snp",
                    "digest": SNP_MEASUREMENT,
                    "vcpu_type": "EPYC-v4",
                }
            ],
        },
    }
    # Fail fast on schema drift, before anything reaches the CCN.
    from aleph_message.models.execution.vprogram import VerifiableProgramContent

    VerifiableProgramContent(**content)
    return content


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
    """The node dict whose v_programs bucket contains item_hash, else None.

    Walks the plan defensively instead of pinning its exact shape: any dict
    with a `v_programs` list containing the hash is the assignment.
    """
    stack = [plan]
    while stack:
        node = stack.pop()
        if isinstance(node, dict):
            bucket = node.get("v_programs")
            if isinstance(bucket, list) and item_hash in bucket:
                return node
            stack.extend(node.values())
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
    assert "EPYC-v4" in models, f"SNP CRN vcpu models: {models} (usage properties: {usage.get('properties')})"


def test_vprogram_scheduled_to_snp_crn(ccn_url, private_key, scheduler_api_url, crn_ssh_key):
    test_start = time.time()
    content = build_vprogram_content(_address_of(private_key))
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
            lambda: _find_assignment(_http_json(f"{scheduler_api_url}/api/v0/plan"), item_hash),
            timeout=300,
            interval=10,
        )
        assignment_text = json.dumps(assignment)
        assert SNP_HOST in assignment_text, (
            f"v-program assigned, but not to the SNP CRN ({SNP_HOST}): {assignment_text}"
        )

        # 3. The signed allocation reached the CRN, which fetched the message
        #    and failed CLEANLY at the designated launch seam. When the launch
        #    increment lands, replace this block with: wait for the VM to
        #    boot, then attest with aleph-attest-cli and fetch the built-in
        #    workload over the attested channel.
        def crn_reported_not_implemented():
            result = _snp_run(
                crn_ssh_key,
                SNP_HOST,
                f"journalctl -u aleph-vm-agent.service --since '@{int(test_start)}' --no-pager | "
                f"grep -m1 'does not implement the SEV-SNP launch path' || true",
                timeout=60,
            )
            return result.stdout.strip() or None

        line = poll(
            "clean not-implemented error in the CRN agent journal",
            crn_reported_not_implemented,
            timeout=300,
            interval=10,
        )
        assert item_hash in line, f"error line does not mention the v-program: {line}"
    finally:
        forget_vprogram(ccn_url, private_key, item_hash)


def _address_of(private_key: str) -> str:
    from aleph.sdk.chains.ethereum import ETHAccount

    return ETHAccount(private_key).get_address()
