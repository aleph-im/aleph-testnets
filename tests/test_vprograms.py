"""End-to-end V-PROGRAM flow (verifiable SEV-SNP programs).

Deploys the fib-service measured workload with the Rust CLI, waits for the
scheduler to place it on the SEV-SNP TEE server (the only CRN advertising
sev_snp capability), then exercises the attested call path: `vprogram call`
only returns a response body after the guest's SEV-SNP report chain verifies
(AMD ARK/ASK/VCEK), the TLS key is bound to the report, and the launch
measurement matches what `vprogram create` pinned on the message. The CLI
computes those measurements itself at create time from the runtime bundle,
so this also locks the CLI measurement path against what the CRN launches.

Fixture provenance: scripts/vprogram-artifacts.sh (nix-reproducible builds
of aleph-vm od/vprogram-integration).
"""
import json
import os

from tests.test_programs import _parse_json_stream

# SNP guest boot on the TEE server includes downloading the runtime bundle
# from the CCN on first launch; give the scheduler + CRN a wide margin.
CREATE_WAIT_SECS = 900


def _vprogram_message(objs: list[dict]) -> dict:
    """The V-PROGRAM message from a `vprogram create --json` output stream
    (STORE messages for the workload + hash tree come first, the --wait
    payload last)."""
    for obj in objs:
        if "verification" in (obj.get("content") or {}):
            return obj
    raise AssertionError(f"no V-PROGRAM message in CLI output: {objs}")


def test_vprogram_deploy_and_attested_call(
    aleph_cli, vprogram_dir, vprogram_runtime_hash, confidential_crn_host
):
    workload = os.path.join(vprogram_dir, "fib-workload.ext4")

    result = aleph_cli(
        "--json", "vprogram", "create",
        "--workload", workload,
        "--runtime", vprogram_runtime_hash,
        "--chain", "eth",
        "--wait", str(CREATE_WAIT_SECS),
        check=False,
        timeout=CREATE_WAIT_SECS + 300,
    )
    assert result.returncode == 0, f"vprogram create failed: {(result.stderr or '')[-1000:]}"

    objs = _parse_json_stream(result.stdout)
    item_hash = _vprogram_message(objs)["item_hash"]

    ready = objs[-1]
    assert ready.get("ready") is True, (
        f"V-PROGRAM {item_hash} not reachable within {CREATE_WAIT_SECS}s: {ready}"
    )
    endpoint = ready.get("attested_endpoint")
    assert endpoint, (
        "V-PROGRAM is running but no attested endpoint was resolved — "
        "is the CRN mapping the :8443 attestation port (aleph-vm#1079)?"
    )
    # V-PROGRAMs are SEV-SNP only: placement anywhere but the TEE server
    # means the scheduler's capability matching regressed.
    assert confidential_crn_host in endpoint, (
        f"attested endpoint {endpoint} is not on the TEE server {confidential_crn_host}"
    )

    # show: pinned measurements present, CRN reports the VM as running.
    shown = aleph_cli("vprogram", "show", item_hash, parse_json=True)
    assert shown["measurements"], "no measurements pinned on the message"
    assert shown["running"] is True, f"CRN does not report the VM as active: {shown}"

    # Attested calls: the response body is only ever printed after the full
    # RA-TLS verification (report chain, key binding, measurement pin).
    health = aleph_cli(
        "vprogram", "call", item_hash, "/health", check=False, timeout=120
    )
    assert health.returncode == 0, f"attested /health call failed: {(health.stderr or '')[-1000:]}"
    assert json.loads(health.stdout)["status"] == "ok"

    fib = aleph_cli("vprogram", "call", item_hash, "/fib/10", check=False, timeout=120)
    assert fib.returncode == 0, f"attested /fib/10 call failed: {(fib.stderr or '')[-1000:]}"
    assert json.loads(fib.stdout)["result"] == 55

    # Fail-closed: a wrong expected measurement must abort the call without
    # ever printing a response body.
    bad = aleph_cli(
        "vprogram", "call", item_hash, "/health",
        "--expected-measurement", "0" * 96,
        check=False, timeout=120,
    )
    assert bad.returncode != 0, "call with a wrong measurement must fail"
    assert not (bad.stdout or "").strip(), (
        "response body must never be printed when attestation fails"
    )
