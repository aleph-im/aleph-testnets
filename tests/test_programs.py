"""Regression tests for PROGRAM messages (on-demand microVMs).

Unlike instances, programs bypass the scheduler entirely: the PROGRAM
message only has to be processed by the CCN, and the first HTTP request to
a CRN's /vm/<item-hash>/ path makes that CRN download the runtime + code
from the CCN, boot the microVM, and proxy the request into the guest.
"""
import json
import os
import urllib.request
import uuid

import pytest

from tests.vm_helpers import poll

DIAGNOSTIC_VM_DIR = os.path.join(os.path.dirname(__file__), "fixtures", "diagnostic_vm")


def _parse_json_stream(stdout: str) -> list[dict]:
    """Parse `program create --json` output: one pretty-printed JSON object per
    submitted message (STORE for the code archive, then PROGRAM), concatenated
    on stdout — so a single json.loads() (the aleph_cli parse_json path) fails
    with 'Extra data'."""
    decoder = json.JSONDecoder()
    objs, idx = [], 0
    while idx < len(stdout):
        obj, end = decoder.raw_decode(stdout, idx)
        objs.append(obj)
        idx = end
        while idx < len(stdout) and stdout[idx].isspace():
            idx += 1
    return objs


def _http_json(url: str, body: dict | None = None, timeout: int = 10) -> dict:
    """GET (or POST, when `body` is given) `url` and parse the JSON response."""
    headers = {"Accept": "application/json"}
    data = None
    if body is not None:
        data = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers)
    resp = urllib.request.urlopen(req, timeout=timeout)
    return json.loads(resp.read())


@pytest.mark.timeout(600)
def test_program_deploy_and_call_endpoints(aleph_cli, program_runtime_hash, crn_url):
    """End-to-end: program create → CRN boots it on first call → endpoints work."""
    marker = uuid.uuid4().hex[:12]

    # Not parse_json=True: create emits two JSON objects (STORE then PROGRAM).
    create_result = aleph_cli(
        "--json", "program", "create",
        DIAGNOSTIC_VM_DIR, "main:app",
        "--name", "diagnostic-vm",
        "--runtime", program_runtime_hash,
        # Explicit sizing: the CLI otherwise requires a `--size` slug, which
        # resolves against the CCN pricing aggregate — absent on a fresh
        # testnet ("Error: --size or --vcpus must be specified").
        "--vcpus", "1",
        "--memory", "512MiB",
        "--env-vars", f"TEST_VAR={marker}",
        "--chain", "eth",
    )
    messages = _parse_json_stream(create_result.stdout)
    program_hash = next(
        (m["item_hash"] for m in messages if m.get("type") == "PROGRAM"), None
    )
    assert program_hash, f"No PROGRAM message in create output: {create_result.stdout[:500]}"

    base = f"{crn_url}/vm/{program_hash}"
    try:
        # First call is the slow path: the CRN fetches the runtime (340 MiB)
        # and code volume from the CCN, then boots the microVM — and it is
        # this very request that triggers the provisioning, which blocks
        # until the VM answers. The long per-request timeout lets one
        # request ride through the whole boot instead of cancelling (and
        # potentially restarting) it every 10 s; the poll stays as the
        # retry envelope. Raising on an unexpected body (rather than
        # returning None) surfaces it in poll's failure message.
        def first_call():
            data = _http_json(f"{base}/", timeout=120)
            if data.get("app") != "diagnostic-vm":
                raise AssertionError(f"unexpected index response: {data!r}")
            return data

        index = poll("Program boot via CRN /vm/ path", first_call, timeout=240, interval=10)
        assert index["status"] == "ok"

        # Query-string plumbing through the CRN proxy.
        echo = _http_json(f"{base}/echo?msg=hello-{marker}")
        assert echo["msg"] == f"hello-{marker}"

        # Request-body plumbing.
        posted = _http_json(f"{base}/echo", body={"marker": marker})
        assert posted["body"] == {"marker": marker}

        # PROGRAM message env vars reach the guest process.
        environ = _http_json(f"{base}/environ")
        assert environ.get("TEST_VAR") == marker
    finally:
        # Best-effort FORGET (also forgets the code STORE). -y: a confirmation
        # prompt on non-TTY stdin reads EOF and silently aborts the FORGET.
        aleph_cli("program", "delete", program_hash, "-y", "--chain", "eth", check=False)
