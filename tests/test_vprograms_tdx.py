"""Intel TDX V-PROGRAM end-to-end: `vprogram create` on a tdx runtime,
scheduler placement onto the TDX host, and attested calls over the TDX
RA-TLS path (quote verified against Intel PCS collateral).

Opt-in only: skips everywhere ALEPH_TESTNET_TDX_CRN_HOST is unset (set only
on TDX-flagged CI runs). Reuses the base V-PROGRAM flow's JSON-stream
parsing and attested-call retry helper from tests/test_vprograms.py.

No IPv6 probe: the host has no global IPv6 (100::/64 pool, like the GPU
host).
"""
import json
import os

from tests.test_programs import _parse_json_stream
from tests.test_vprograms import CREATE_WAIT_SECS, _attested_call_with_retry, _vprogram_message

# TEMPORARY (remove once the host's firmware is current): the PhoenixNAP
# DL320 Gen12 is pinned to ROM 1.10 (newer ROMs refuse SGX/TDX on its
# unvalidated DIMM population), whose microcode and TDX module trail
# Intel's TCB-R, so Intel appraises the platform OutOfDate with eight
# advisories. The CLI verifies everything else (PCK chain, CRLs, QE
# identity, quote signature, key binding, the four register pins) and
# refuses OutOfDate by default; admit it so the flow validation stays
# green. With this flag the run validates the whole TDX flow but NOT the
# TCB gate; drop it once a validated DIMM population unlocks a newer ROM.
TDX_TCB_ARGS = ("--tdx-accept-tcb", "out-of-date")


def test_tdx_vprogram_deploy_and_attested_call(
    aleph_cli, vprogram_dir, vprogram_tdx_runtime_hash, tdx_crn_host, confidential_crn_host
):
    workload = os.path.join(vprogram_dir, "fib-workload.ext4")

    result = aleph_cli(
        "--json", "vprogram", "create", "testnet-fib-tdx",
        "--workload", workload,
        "--runtime", vprogram_tdx_runtime_hash,
        "--memory", "2048",
        "--chain", "eth",
        "--wait", str(CREATE_WAIT_SECS),
        check=False,
        timeout=CREATE_WAIT_SECS + 300,
    )
    assert result.returncode == 0, f"vprogram create (tdx) failed: {(result.stderr or '')[-1000:]}"

    objs = _parse_json_stream(result.stdout)
    message = _vprogram_message(objs)
    item_hash = message["item_hash"]
    verification = json.loads(message["item_content"])["verification"]
    assert verification["backend"] == "tdx", verification
    assert "policy" not in verification, "a tdx block carries no launch policy"
    assert len(verification["measurements"]) == 1, verification
    registers = verification["measurements"][0]["registers"]
    assert set(registers) == {"mrtd", "rtmr1", "rtmr2", "mrconfigid"}, registers

    ready = objs[-1]
    assert ready.get("ready") is True, (
        f"TDX V-PROGRAM {item_hash} not reachable within {CREATE_WAIT_SECS}s: {ready}"
    )
    endpoint = ready.get("attested_endpoint")
    assert endpoint, "TDX V-PROGRAM is running but no attested endpoint was resolved"
    # Placement: a tdx backend lands only on a CRN advertising tee.tdx, so
    # never on the SNP TEE server.
    assert tdx_crn_host in endpoint, (
        f"attested endpoint {endpoint} is not on the TDX host {tdx_crn_host}"
    )
    assert confidential_crn_host not in endpoint, (
        f"TDX V-PROGRAM placed on the SNP TEE server {confidential_crn_host}"
    )

    shown = aleph_cli("vprogram", "show", item_hash, parse_json=True)
    assert shown["measurements"], "no measurements pinned on the message"
    assert shown["running"] is True, f"CRN does not report the VM as active: {shown}"

    # Attested calls: the body is only printed after the quote verified
    # (chain, collateral, TCB under the policy, key binding, register pin).
    health = _attested_call_with_retry(aleph_cli, item_hash, "/health", endpoint, *TDX_TCB_ARGS)
    assert json.loads(health.stdout)["status"] == "ok"

    fib = aleph_cli(
        "--json", "vprogram", "call", item_hash, "/fib/10", *TDX_TCB_ARGS,
        check=False, timeout=120,
    )
    assert fib.returncode == 0, f"attested /fib/10 call failed: {(fib.stderr or '')[-1000:]}"
    verdict = json.loads(fib.stdout)
    assert verdict["backend"] == "tdx", verdict
    assert verdict["body"]["result"] == 55, verdict
    assert verdict["freshness"] == "verified", verdict
    # The registers the guest presented are the ones the message pins.
    assert verdict["registers"] == registers, verdict
    assert verdict["tcb_status"] == "OutOfDate", verdict

    # Negative: the default policy refuses this host's TCB, so the same
    # call without the override must fail before printing a body.
    refused = aleph_cli(
        "vprogram", "call", item_hash, "/fib/10",
        check=False, timeout=120,
    )
    assert refused.returncode != 0, "the default TCB policy must refuse an OutOfDate platform"
    assert "OutOfDate" in (refused.stderr or ""), (refused.stderr or "")[-1000:]
