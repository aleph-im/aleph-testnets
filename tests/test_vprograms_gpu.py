"""GPU V-PROGRAM end-to-end: `vprogram create --gpu`, scheduler placement onto
the NVIDIA CC host, and attested CUDA calls over the SEV-SNP RA-TLS path.

Opt-in only: skips everywhere ALEPH_TESTNET_NVIDIA_CC_CRN_HOST is unset (set
only on GPU-flagged CI runs). Reuses the base V-PROGRAM flow's JSON-stream
parsing and attested-call retry helper from tests/test_vprograms.py.

No TCB floor override here: this host's microcode is current. No IPv6 probe.
"""
import json
import os

from tests.test_programs import _parse_json_stream
from tests.test_vprograms import CREATE_WAIT_SECS, _attested_call_with_retry, _vprogram_message

# The NVIDIA CC host's card. Pinning it exercises the guest's signed board
# identity check, not only the scheduler's device id match.
H200_NVL_PCI_ID = "10de:233b"


def test_gpu_vprogram_deploy_and_attested_cuda_call(
    aleph_cli, vprogram_dir, vprogram_gpu_runtime_hash, nvidia_cc_crn_host, confidential_crn_host
):
    workload = os.path.join(vprogram_dir, "cuda-workload.ext4")

    item_hash = None
    bw_item_hash = None
    try:
        result = aleph_cli(
            "--json", "vprogram", "create", "testnet-cuda",
            "--workload", workload,
            "--runtime", vprogram_gpu_runtime_hash,
            "--gpu", "hopper",
            "--gpu-model", H200_NVL_PCI_ID,
            "--memory", "4096",
            "--chain", "eth",
            "--wait", str(CREATE_WAIT_SECS),
            check=False,
            timeout=CREATE_WAIT_SECS + 300,
        )
        assert result.returncode == 0, f"vprogram create --gpu failed: {(result.stderr or '')[-1000:]}"

        objs = _parse_json_stream(result.stdout)
        item_hash = _vprogram_message(objs)["item_hash"]

        ready = objs[-1]
        assert ready.get("ready") is True, (
            f"GPU V-PROGRAM {item_hash} not reachable within {CREATE_WAIT_SECS}s: {ready}"
        )
        endpoint = ready.get("attested_endpoint")
        assert endpoint, "GPU V-PROGRAM is running but no attested endpoint was resolved"
        # Placement: this V-PROGRAM needs a GPU, so it must land on the
        # NVIDIA CC host, never on the plain SNP TEE server.
        assert nvidia_cc_crn_host in endpoint, (
            f"attested endpoint {endpoint} is not on the NVIDIA CC host {nvidia_cc_crn_host}"
        )
        assert confidential_crn_host not in endpoint, (
            f"GPU V-PROGRAM placed on the non-GPU TEE server {confidential_crn_host}"
        )

        shown = aleph_cli("vprogram", "show", item_hash, parse_json=True)
        assert shown["measurements"], "no measurements pinned on the message"
        assert shown["running"] is True, f"CRN does not report the VM as active: {shown}"
        assert shown.get("gpu") == {
            "vendor": "nvidia", "arch": "hopper", "count": 1,
            "models": [H200_NVL_PCI_ID], "mode": "cc",
        }, f"unexpected gpu requirement dict: {shown.get('gpu')}"

        gpu_probe = _attested_call_with_retry(aleph_cli, item_hash, "/gpu", endpoint)
        gpu_body = json.loads(gpu_probe.stdout)
        assert gpu_body["ok"] is True, f"/gpu probe not ok: {gpu_body}"
        assert gpu_body["kernel"] == "saxpy", f"unexpected kernel: {gpu_body}"
        assert "H200" in gpu_body["device"], f"unexpected device: {gpu_body}"

        bw_probe = _attested_call_with_retry(aleph_cli, item_hash, "/bandwidth?mib=1024", endpoint)
        bw_body = json.loads(bw_probe.stdout)
        assert bw_body["ok"] is True, f"/bandwidth probe not ok: {bw_body}"

        # Negative: no Blackwell card exists on this host, so the create must
        # not report ready within its wait budget. A non-zero exit or a
        # ready=false payload are both the expected outcome; only an
        # unexpected ready=true is a failure.
        bw_create = aleph_cli(
            "--json", "vprogram", "create", "testnet-cuda-bw",
            "--workload", workload,
            "--runtime", vprogram_gpu_runtime_hash,
            "--gpu", "blackwell",
            "--memory", "4096",
            "--chain", "eth",
            "--wait", "120",
            check=False,
            timeout=420,
        )
        bw_objs = _parse_json_stream(bw_create.stdout) if bw_create.stdout.strip() else []
        bw_msg = next((o for o in bw_objs if o.get("type") == "V-PROGRAM"), None)
        if bw_msg:
            bw_item_hash = bw_msg["item_hash"]
        bw_ready = bw_objs[-1] if bw_objs else {}
        assert not (bw_create.returncode == 0 and bw_ready.get("ready") is True), (
            f"Blackwell V-PROGRAM unexpectedly became ready: {bw_ready}"
        )
    finally:
        if item_hash:
            aleph_cli("vprogram", "delete", item_hash, "-y", check=False)
        if bw_item_hash:
            aleph_cli("vprogram", "delete", bw_item_hash, "-y", check=False)
