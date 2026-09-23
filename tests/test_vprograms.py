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
of aleph-vm, currently the 2026.08.31 "1.1" runtime from c5391963).
"""
import json
import os
import subprocess
import time

from tests.test_programs import _parse_json_stream

# SNP guest boot on the TEE server includes downloading the runtime bundle
# from the CCN on first launch; give the scheduler + CRN a wide margin.
CREATE_WAIT_SECS = 900

# TEMPORARY (remove once Scaleway updates the new TEE server's BIOS): the
# TEE server that replaced the previous one on 2026-09-09 (another Dell
# C6615, BIOS 1.3.3, Siena/Zen4c) was delivered with x86 microcode SPL 21,
# below the EntrySign mitigation minimum of 25 (AMD-SB-3019, the CLI's
# Zen4c floor), and Elastic Metal users cannot flash the BIOS themselves.
# Accept the known-outdated host so the flow validation stays green; the
# CLI still prints its "guest may be exposed" warning on every call. With
# these flags the run validates the whole V-PROGRAM flow but NOT the TCB
# gate; drop them to restore the real floor.
TCB_FLOOR_ARGS = ("--min-tcb", "microcode=21", "--accept-outdated-tcb")


def _vprogram_message(objs: list[dict]) -> dict:
    """The V-PROGRAM submission receipt from a `vprogram create --json`
    output stream (the --wait payload comes last)."""
    for obj in objs:
        if obj.get("type") == "V-PROGRAM":
            return obj
    raise AssertionError(f"no V-PROGRAM message in CLI output: {objs}")


def _attested_call_with_retry(aleph_cli, item_hash, path, endpoint, *extra_args, deadline_secs=120):
    """`vprogram call <item_hash> <path>`, retrying transport-level failures.

    The attestation port is mapped as soon as the VM reaches RUNNING, a few
    seconds before the guest's attest agent binds it (run 31382627461: call
    at 11:46:49, guest bind at 11:46:52), so retry transport failures
    briefly. Once the agent is up it answers a workload that is not yet
    listening with 503 "upstream not ready" (aleph-vm#1282; 502 "upstream
    unreachable" before) while the CLI exits 0: retry those too.
    Verification failures still fail fast.
    """
    deadline = time.time() + deadline_secs
    curl_probe = None
    while True:
        result = aleph_cli(
            "vprogram", "call", item_hash, path, *extra_args,
            check=False, timeout=120,
        )
        body = result.stdout or ""
        starting = "upstream not ready" in body or "upstream unreachable" in body
        if result.returncode == 0 and not starting:
            return result
        if result.returncode != 0 and curl_probe is None:
            # Transport ground truth, captured in the same seconds the CLI
            # fails: a raw TLS request with NO attestation verification.
            # 200 here + a CLI failure isolates the failure to attestation
            # itself; a curl failure means the endpoint genuinely is not
            # reachable.
            probe_url = endpoint.rstrip("/") + path
            p = subprocess.run(
                ["curl", "-ks", "-o", "/dev/null", "-w", "%{http_code}", probe_url],
                capture_output=True, text=True, timeout=15,
            )
            curl_probe = p.stdout.strip() or "no-response"
        transient = starting or "error sending request" in (result.stderr or "")
        if not transient or time.time() >= deadline:
            raise AssertionError(
                f"attested {path} call failed (unverified curl probe of the same "
                f"endpoint: HTTP {curl_probe}): {(result.stderr or '')[-1000:]}"
            )
        time.sleep(5)


def test_vprogram_deploy_and_attested_call(
    aleph_cli, vprogram_dir, vprogram_runtime_hash, confidential_crn_host, tee_pin_args
):
    workload = os.path.join(vprogram_dir, "fib-workload.ext4")

    result = aleph_cli(
        # aleph-cli 0.17.0 (aleph-rs#361): the name is a mandatory positional.
        "--json", "vprogram", "create", "testnet-fib",
        "--workload", workload,
        "--runtime", vprogram_runtime_hash,
        "--chain", "eth",
        "--wait", str(CREATE_WAIT_SECS),
        *tee_pin_args,
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
    # rc2's create --wait polls for the endpoint within the wait budget
    # (aleph-rs#318), so a ready payload without one is a real failure.
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
    health = _attested_call_with_retry(aleph_cli, item_hash, "/health", endpoint, *TCB_FLOOR_ARGS)
    assert json.loads(health.stdout)["status"] == "ok"

    fib = aleph_cli(
        "vprogram", "call", item_hash, "/fib/10", *TCB_FLOOR_ARGS,
        check=False, timeout=120,
    )
    assert fib.returncode == 0, f"attested /fib/10 call failed: {(fib.stderr or '')[-1000:]}"
    assert json.loads(fib.stdout)["result"] == 55

    # IPv6 reachability. The CRN derives and publishes the guest's IPv6 up
    # front; the guest leases exactly that address via per-tap DHCPv6 with
    # its default route from the RA, ndppd proxies the guest range on the
    # uplink, and the attest agent binds [::]:8443 dual-stack
    # (aleph-vm#1087, #1125, #1126). Probe the published address over raw
    # TLS: reachability only, the attested path is already exercised via
    # the mapped IPv4 port above. Asserted since run 32184087567 went
    # green end to end; requires the CRN's pool to be a prefix whose
    # VM-sourced egress the provider permits (on Scaleway Elastic Metal:
    # an attached flexible-IP /64, NOT the native /64 — see crn-up.sh).
    guest_ipv6 = shown.get("ipv6_ip")
    assert guest_ipv6, f"CRN published no ipv6_ip for the V-PROGRAM: {shown}"
    probe_url = f"https://[{guest_ipv6}]:8443/health"
    deadline = time.time() + 60
    code = "no-response"
    while time.time() < deadline:
        probe = subprocess.run(
            ["curl", "-ks", "-6", "-o", "/dev/null", "-w", "%{http_code}",
             "--max-time", "10", probe_url],
            capture_output=True, text=True, timeout=20,
        )
        code = probe.stdout.strip() or "no-response"
        if code == "200":
            break
        time.sleep(5)
    assert code == "200", (
        f"V-PROGRAM IPv6 unreachable: {probe_url} -> HTTP {code} (guest "
        f"lease, RA route, ndppd proxying and provider egress all need to "
        f"hold; see the tee-network.txt diagnostics artifact)"
    )

    # Fail-closed: a wrong expected measurement must abort the call without
    # ever printing a response body.
    # TCB_FLOOR_ARGS here too, so this fails on the measurement pin rather
    # than on the host's outdated microcode.
    bad = aleph_cli(
        "vprogram", "call", item_hash, "/health",
        "--expected-measurement", "0" * 96, *TCB_FLOOR_ARGS,
        check=False, timeout=120,
    )
    assert bad.returncode != 0, "call with a wrong measurement must fail"
    assert not (bad.stdout or "").strip(), (
        "response body must never be printed when attestation fails"
    )
