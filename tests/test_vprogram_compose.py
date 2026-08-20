"""End-to-end aleph.compose/1 V-PROGRAM flow (docker-compose workloads).

Deploys a minimal single-service docker-compose stack with the Rust CLI:
`vprogram create --compose` validates the compose subset, pulls and
digest-pins the image, builds the measured workload volume (compose file +
OCI archive on ext4), verity-hashes it and pins the workload-bound SEV-SNP
measurement on the message. The CRN boots the compose runtime (podman +
podman-compose platform rootfs), whose guest init verity-mounts the
workload volume, loads the image archives and brings the stack up with
host networking; the attest agent proxies 127.0.0.1:8080 behind the
attested :8443 endpoint.

The workload is traefik/whoami serving on :8080: a stock Docker Hub image,
so this also exercises the registry pull + digest-pin + in-guest
`podman load` matching path end to end.

Fixture provenance: scripts/vprogram-artifacts.sh (the 2026.08.20 compose
runtime from aleph-vm ba690c65).
"""
import subprocess
import time

from tests.test_programs import _parse_json_stream
from tests.test_vprograms import CREATE_WAIT_SECS, TCB_FLOOR_ARGS, _vprogram_message

# One service, host networking (required by the aleph.compose/1 subset),
# serving plain HTTP on the runtime's fixed 127.0.0.1:8080 upstream.
COMPOSE_YML = """\
services:
  whoami:
    image: traefik/whoami:v1.10.2
    command: ["--port", "8080"]
    network_mode: host
"""


def test_vprogram_compose_deploy_and_attested_call(
    aleph_cli, vprogram_compose_runtime_hash, confidential_crn_host, tmp_path
):
    compose_file = tmp_path / "docker-compose.yml"
    compose_file.write_text(COMPOSE_YML)

    result = aleph_cli(
        "--json", "vprogram", "create",
        "--compose", str(compose_file),
        "--runtime", vprogram_compose_runtime_hash,
        "--chain", "eth",
        "--wait", str(CREATE_WAIT_SECS),
        check=False,
        timeout=CREATE_WAIT_SECS + 300,
    )
    assert result.returncode == 0, f"vprogram create --compose failed: {(result.stderr or '')[-1000:]}"

    objs = _parse_json_stream(result.stdout)
    item_hash = _vprogram_message(objs)["item_hash"]

    ready = objs[-1]
    assert ready.get("ready") is True, (
        f"compose V-PROGRAM {item_hash} not reachable within {CREATE_WAIT_SECS}s: {ready}"
    )
    endpoint = ready.get("attested_endpoint")
    assert endpoint, "compose V-PROGRAM is running but no attested endpoint was resolved"
    assert confidential_crn_host in endpoint, (
        f"attested endpoint {endpoint} is not on the TEE server {confidential_crn_host}"
    )

    shown = aleph_cli("vprogram", "show", item_hash, parse_json=True)
    assert shown["measurements"], "no measurements pinned on the message"
    assert shown["running"] is True, f"CRN does not report the VM as active: {shown}"

    # Attested call through the RA-TLS channel. The compose guest has real
    # startup work left after the attestation port maps: verity-mount the
    # workload volume, podman-load the image archives, compose up. During
    # that window the attest agent is already up and answers with an
    # upstream-unreachable 502 body while the CLI exits 0 (attestation
    # itself succeeded; run 32372006187), so BOTH transport failures
    # (rc != 0) and 502 bodies are the not-ready-yet signal. Verification
    # failures fail fast.
    deadline = time.time() + 240
    curl_probe = None
    while True:
        root = aleph_cli(
            "vprogram", "call", item_hash, "/", *TCB_FLOOR_ARGS,
            check=False, timeout=120,
        )
        body = root.stdout or ""
        if root.returncode == 0 and "Hostname:" in body:
            break
        transient_transport = root.returncode != 0 and "error sending request" in (root.stderr or "")
        stack_starting = root.returncode == 0 and "upstream unreachable" in body
        if transient_transport and curl_probe is None:
            # Transport ground truth with NO attestation verification, to
            # isolate attestation failures from plain unreachability.
            probe_url = endpoint.rstrip("/") + "/"
            p = subprocess.run(
                ["curl", "-ks", "-o", "/dev/null", "-w", "%{http_code}", probe_url],
                capture_output=True, text=True, timeout=15,
            )
            curl_probe = p.stdout.strip() or "no-response"
        if not (transient_transport or stack_starting) or time.time() >= deadline:
            raise AssertionError(
                f"attested / call did not return the whoami dump within the "
                f"startup budget (unverified curl probe: HTTP {curl_probe}): "
                f"stdout={body[:500]!r} stderr={(root.stderr or '')[-500:]!r}"
            )
        time.sleep(5)

    # whoami's / dumps request/host info; Hostname proves the container
    # itself answered through the attested proxy, not some error page.
    assert "Hostname:" in root.stdout
