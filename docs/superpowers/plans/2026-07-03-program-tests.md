# PROGRAM Message Regression Tests Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Regression-test PROGRAM messages by deploying a diagnostic FastAPI microVM and calling its endpoints through the CRN's `/vm/<item-hash>/` path.

**Architecture:** A vendored ~40-line FastAPI app is deployed with `aleph program create` against a runtime squashfs seeded from mainnet IPFS onto the testnet CCN. The test polls the CRN's on-demand `/vm/<hash>/` endpoint (first request triggers asset download + microVM boot — no scheduler involved) and asserts query-string, request-body, and env-var plumbing.

**Tech Stack:** pytest (+pytest-timeout), aleph CLI ≥0.11.1 (pinned in manifesto.yml), urllib (stdlib — no new deps), bash (scripts/local-up.sh).

**Spec:** `docs/superpowers/specs/2026-07-03-program-tests-design.md`

## Global Constraints

- Runtime: official `aleph-debian-12-python`, IPFS CID `QmXb4khKJJazpEuGVzchSy6yeJubGf8gy9Qjd4ZGSY6hXZ` (mainnet STORE message `63f07193e6ee9d207b7d1fcf8286f9aee34e6f12f101d2ec77c1229f92964696`), 340 MiB, cached at `.local/runtime.squashfs`.
- Env var contract: `ALEPH_TESTNET_PROGRAM_RUNTIME` = path to the runtime squashfs; tests `pytest.skip` when unset/missing (mirrors `ALEPH_TESTNET_ROOTFS`).
- `--runtime <hash>` must be passed explicitly to `program create`: the default resolves the `vm-images` aggregate, which does not exist on a fresh testnet CCN.
- All CLI signing commands pass `--chain eth`; deletes pass `-y` (a confirmation prompt on non-TTY stdin reads EOF and silently aborts).
- Test helpers: reuse `tests/vm_helpers.py` (`poll`, `NODESTATUS_ADDR`); import style is absolute (`from tests.vm_helpers import …`).
- No new Python dependencies; HTTP via stdlib `urllib`.
- No `Co-Authored-By` trailer on commits.

---

### Task 1: Vendored diagnostic VM app

**Files:**
- Create: `tests/fixtures/__init__.py` (empty — keeps pytest collection happy since `tests/` is a package)
- Create: `tests/fixtures/diagnostic_vm/main.py`

**Interfaces:**
- Produces: an ASGI app importable as entrypoint `main:app` inside the aleph runtime. Endpoints consumed by Task 4: `GET /` → `{"app": "diagnostic-vm", "status": "ok"}`; `GET /echo?msg=X` → `{"msg": "X"}`; `POST /echo` (JSON body) → `{"body": <body>}`; `GET /environ` → `dict(os.environ)`.

- [ ] **Step 1: Create the fixture package and app**

`tests/fixtures/__init__.py`: empty file.

`tests/fixtures/diagnostic_vm/main.py`:

```python
"""Diagnostic microVM for PROGRAM message regression tests.

A minimal stand-in for aleph-vm's examples/example_fastapi: just enough
endpoints to prove the CRN's /vm/<hash>/ proxy plumbs paths, query strings,
request bodies, and PROGRAM-message env vars into the guest. Imports only
FastAPI (bundled in the aleph-debian-12-python runtime) and stdlib, so it
runs unmodified in the runtime with no code volume dependencies.
Entrypoint: `main:app`.
"""
import os

from fastapi import FastAPI

app = FastAPI()


@app.get("/")
async def index():
    return {"app": "diagnostic-vm", "status": "ok"}


@app.get("/echo")
async def echo_query(msg: str = ""):
    return {"msg": msg}


@app.post("/echo")
async def echo_body(body: dict):
    return {"body": body}


@app.get("/environ")
async def environ():
    return dict(os.environ)
```

- [ ] **Step 2: Verify the file is valid Python**

FastAPI is not installed on the host, so don't import it — byte-compile only:

Run: `python3 -m py_compile tests/fixtures/diagnostic_vm/main.py && echo OK`
Expected: `OK`

- [ ] **Step 3: Commit**

```bash
git add tests/fixtures/__init__.py tests/fixtures/diagnostic_vm/main.py
git commit -m "test: vendor diagnostic FastAPI app for program tests"
```

---

### Task 2: Runtime seeding in scripts/local-up.sh

**Files:**
- Modify: `scripts/local-up.sh:101-113` (add `download_runtime()` after `download_rootfs()`)
- Modify: `scripts/local-up.sh:143-145` (call it in `deploy_contracts()`)
- Modify: `scripts/local-up.sh:211-224` (`run_tests()` env export)
- Modify: `scripts/local-up.sh:283-308` (CLI dispatch: `--download-runtime` flag + both usage lines)

Line numbers are anchors as of commit `e95e44d`; locate by content.

**Interfaces:**
- Produces: `.local/runtime.squashfs` on disk; `ALEPH_TESTNET_PROGRAM_RUNTIME` exported for pytest (consumed by Task 3's `program_runtime` fixture).

- [ ] **Step 1: Add `download_runtime()` directly below `download_rootfs()`**

```bash
# Official aleph-debian-12-python runtime (mainnet STORE message 63f07193…,
# stored on IPFS). Programs reference a runtime by item hash and the CRN
# downloads it from the testnet CCN, so the tests upload this file there.
RUNTIME_IPFS_CID="QmXb4khKJJazpEuGVzchSy6yeJubGf8gy9Qjd4ZGSY6hXZ"

download_runtime() {
    local runtime="$LOCAL_DIR/runtime.squashfs"
    if [ -f "$runtime" ]; then
        echo "==> Program runtime already downloaded"
        return
    fi
    echo "==> Downloading aleph-debian-12-python runtime (340 MiB)..."
    mkdir -p "$LOCAL_DIR"
    curl -fSL -o "$runtime" \
        "https://ipfs.aleph.im/ipfs/$RUNTIME_IPFS_CID"
    echo "==> Runtime downloaded to $runtime"
}
```

- [ ] **Step 2: Call it wherever `download_rootfs` is called**

In `deploy_contracts()` (currently line 143–144):

```bash
deploy_contracts() {
    download_rootfs
    download_runtime
```

In the CLI dispatch `case` (after the `--download-rootfs)` arm):

```bash
    --download-runtime)
        download_runtime
        ;;
```

Update **both** usage lines (currently 303 and 307) to include `--download-runtime` after `--download-rootfs`:

```bash
        echo "Usage: $0 [--env|--up|--deploy-contracts|--download-rootfs|--download-runtime|--crn-up|--crn-down|--test|--logs|--down]"
```

- [ ] **Step 3: Export the env var in `run_tests()`**

Directly below `export ALEPH_TESTNET_ROOTFS="$LOCAL_DIR/rootfs.img"` (line 220):

```bash
    export ALEPH_TESTNET_PROGRAM_RUNTIME="$LOCAL_DIR/runtime.squashfs"
```

- [ ] **Step 4: Verify shell syntax**

Run: `bash -n scripts/local-up.sh && echo OK`
Expected: `OK`

- [ ] **Step 5: Commit**

```bash
git add scripts/local-up.sh
git commit -m "feat: seed aleph program runtime for program tests"
```

---

### Task 3: Session fixtures in tests/conftest.py

**Files:**
- Modify: `tests/conftest.py` (three fixtures after `rootfs_hash`, ~line 263; one import at top)

**Interfaces:**
- Consumes: `_upload_with_balance_retry(aleph_cli, path, what)` (already in conftest.py:298), `NODESTATUS_ADDR` from `tests/vm_helpers.py`.
- Produces (consumed by Task 4): `program_runtime: str` (path), `program_runtime_hash: str` (item hash on the testnet CCN), `crn_url: str` (e.g. `http://1.2.3.4:4020`, no trailing slash).

- [ ] **Step 1: Add the import at the top of conftest.py**

```python
from tests.vm_helpers import NODESTATUS_ADDR
```

- [ ] **Step 2: Add fixtures after `rootfs_hash` (below line 262)**

Note: `_upload_with_balance_retry` is defined lower in the file (line ~298) — that's fine, it's resolved at call time.

```python
@pytest.fixture(scope="session")
def program_runtime() -> str:
    path = os.environ.get("ALEPH_TESTNET_PROGRAM_RUNTIME", "")
    if not path or not os.path.exists(path):
        pytest.skip("No program runtime — program tests require ALEPH_TESTNET_PROGRAM_RUNTIME")
    return path


@pytest.fixture(scope="session")
def program_runtime_hash(aleph_cli, program_runtime) -> str:
    """Upload the runtime squashfs once per session; return its item_hash.

    Must be referenced explicitly at `program create` time: the CLI's default
    runtime resolution reads the `vm-images` aggregate, which does not exist
    on a fresh testnet CCN, and the CRN downloads the runtime by this hash."""
    return _upload_with_balance_retry(aleph_cli, program_runtime, "Program runtime")


@pytest.fixture(scope="session")
def crn_url(aleph_cli) -> str:
    """Corechannel-registered API URL of the first CRN (e.g. http://1.2.3.4:4020).

    Programs run on-demand on whichever CRN receives the HTTP call — no
    scheduler placement to resolve, so any registered CRN will do. (The
    `crn_nodes` fixture instead demands >=2 nodes, for migration tests.)"""
    nodes = aleph_cli(
        "node", "list",
        "--type", "crn",
        "--all",
        "--corechannel-address", NODESTATUS_ADDR,
        parse_json=True,
    )
    if not nodes:
        pytest.skip("No CRNs registered — program tests require a registered CRN")
    return nodes[0]["address"].rstrip("/")
```

- [ ] **Step 3: Verify collection still works**

Run: `~/.local/bin/pytest tests/ --collect-only -q 2>&1 | tail -3`
Expected: a test count, no collection errors.

- [ ] **Step 4: Commit**

```bash
git add tests/conftest.py
git commit -m "test: session fixtures for program runtime + CRN URL"
```

---

### Task 4: The program regression test

**Files:**
- Create: `tests/test_programs.py`

**Interfaces:**
- Consumes: `program_runtime_hash`, `crn_url` (Task 3); `poll` from `tests/vm_helpers.py`; the endpoint contract from Task 1; `aleph_cli` (conftest).

- [ ] **Step 1: Write the test**

`tests/test_programs.py`:

```python
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
        # and code volume from the CCN, then boots the microVM.
        def first_call():
            data = _http_json(f"{base}/")
            return data if data.get("app") == "diagnostic-vm" else None

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
```

- [ ] **Step 2: Verify skip behavior without a testnet**

Run: `ALEPH_TESTNET_CCN_URL=http://localhost:1 ALEPH_TESTNET_PRIVATE_KEY=0x01 ~/.local/bin/pytest tests/test_programs.py -v 2>&1 | tail -5`
Expected: the test errors/skips at the `ccn_ready` autouse fixture (CCN unreachable) — proving collection and imports are sound. With a reachable CCN but `ALEPH_TESTNET_PROGRAM_RUNTIME` unset, it must report `SKIPPED` (verified in Task 5).

- [ ] **Step 3: Verify collection**

Run: `~/.local/bin/pytest tests/test_programs.py --collect-only -q`
Expected: `tests/test_programs.py::test_program_deploy_and_call_endpoints` listed, exit 0.

- [ ] **Step 4: Commit**

```bash
git add tests/test_programs.py
git commit -m "test: PROGRAM message regression test via CRN /vm/ path"
```

---

### Task 5: End-to-end verification against a live testnet

**Files:** none (verification only).

No testnet is currently running (no `.local/` state; known droplet IPs unreachable), so this task needs one provisioned. This is the same cost profile as the existing instance tests: a local docker CCN stack plus one DigitalOcean CRN droplet.

- [ ] **Step 1: Provision (skip if a testnet is already up)**

```bash
./scripts/local-up.sh --env && ./scripts/local-up.sh --up && ./scripts/local-up.sh --deploy-contracts
CCN_URL=http://<host-ip>:4024 ./scripts/crn-up.sh   # needs DO_SSH_KEY_FINGERPRINT
```

- [ ] **Step 2: Confirm skip contract**

Run: `ALEPH_TESTNET_PROGRAM_RUNTIME= ./scripts/local-up.sh --test tests/test_programs.py -v` (or unset the var and run pytest directly)
Expected: `SKIPPED [1] ... No program runtime`

- [ ] **Step 3: Run the program test for real**

Run: `./scripts/local-up.sh --test tests/test_programs.py -v`
Expected: `1 passed` within the 600 s timeout. On failure, check CRN logs: `ssh root@<crn-ip> journalctl -u aleph-vm-supervisor -n 200`.

- [ ] **Step 4: Run the full suite to check for regressions**

Run: `./scripts/local-up.sh --test`
Expected: no new failures relative to the pre-change baseline.

- [ ] **Step 5: Tear down (if provisioned only for this)**

```bash
./scripts/local-up.sh --down
```
