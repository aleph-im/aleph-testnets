# PROGRAM message regression tests — design

**Date:** 2026-07-03
**Status:** approved

## Goal

The test suite covers INSTANCE messages end-to-end but has no coverage for
PROGRAM messages (on-demand microVMs). Add a regression test that deploys a
diagnostic VM (a small FastAPI app, modeled on aleph-vm's `example_fastapi`)
and calls its endpoints through the CRN's `/vm/<item-hash>/` path.

This exercises the PROGRAM-specific pipeline that instance tests do not:
CCN processing of PROGRAM + code STORE messages, CRN on-demand scheduling
(no scheduler involvement — the first HTTP request triggers asset download
and microVM boot), and the CRN's HTTP proxy into the guest.

## Components

### 1. Runtime seeding (`scripts/local-up.sh`)

PROGRAM messages reference a runtime squashfs by item hash, and the CRN
downloads it from its configured CCN (`ALEPH_VM_API_SERVER` points at the
testnet CCN). A fresh testnet has no runtime, so we seed the official
`aleph-debian-12-python` runtime (bundles FastAPI; 340 MiB):

- `download_runtime()` alongside `download_rootfs()`: fetch
  `https://ipfs.aleph.im/ipfs/QmXb4khKJJazpEuGVzchSy6yeJubGf8gy9Qjd4ZGSY6hXZ`
  (the IPFS CID behind mainnet STORE message `63f07193…`) into
  `.local/runtime.squashfs`, cached like `rootfs.img`.
- Called wherever `download_rootfs` is called; a `--download-runtime` flag
  mirrors `--download-rootfs`.
- `run_tests()` exports `ALEPH_TESTNET_PROGRAM_RUNTIME="$LOCAL_DIR/runtime.squashfs"`.

### 2. Vendored diagnostic app (`tests/fixtures/diagnostic_vm/main.py`)

A ~40-line FastAPI app (hermetic — committed to this repo, no dependency on
the aleph-vm repo at test time). Endpoints:

- `GET /` — index JSON marker (`{"app": "diagnostic-vm", "status": "ok"}`)
- `GET /echo?msg=…` — echoes the query parameter (query-string plumbing
  through the CRN proxy)
- `POST /echo` — echoes the JSON request body (request-body plumbing)
- `GET /environ` — returns `dict(os.environ)` (env-var injection)

Imports only `fastapi` + stdlib; the seeded runtime provides FastAPI.

### 3. Fixtures (`tests/conftest.py`)

Follow the exact `rootfs_image`/`rootfs_hash` pattern:

- `program_runtime` (session): path from `ALEPH_TESTNET_PROGRAM_RUNTIME`,
  `pytest.skip` when unset/missing.
- `program_runtime_hash` (session): upload once per session via
  `_upload_with_balance_retry`.
- `crn_url` (session): first registered CRN's corechannel address
  (e.g. `http://1.2.3.4:4020`) via `node list`; `pytest.skip` when no CRN is
  registered. (The existing `crn_nodes` fixture demands ≥2 nodes for
  migration tests — programs need only one.)

### 4. The test (`tests/test_programs.py`)

One end-to-end test, `@pytest.mark.timeout(600)`:

1. `aleph program create tests/fixtures/diagnostic_vm main:app
   --runtime <program_runtime_hash> --env-vars TEST_VAR=<unique>
   --chain eth --json` → program item hash. The CLI auto-uploads the code
   directory as a STORE message. `--runtime` must be explicit: the CLI's
   default resolves the `vm-images` aggregate, which does not exist on a
   fresh testnet (same reason `confidential_firmware_hash` is explicit).
2. Poll `GET {crn_url}/vm/<program_hash>/` (via `vm_helpers.poll`, urllib)
   until HTTP 200 — the first request makes the CRN fetch the runtime + code
   from the CCN and boot the microVM, so the poll window is generous (240 s).
   Assert the index JSON marker.
3. Assert endpoint plumbing:
   - `GET …/echo?msg=hello-<unique>` echoes the value,
   - `POST …/echo` with a JSON body echoes it back,
   - `GET …/environ` contains `TEST_VAR=<unique>` (env vars from the PROGRAM
     message reach the guest).
4. Teardown (try/finally): best-effort `aleph program delete <hash> -y
   --chain eth` (`check=False`; `-y` because a confirmation prompt on
   non-TTY stdin reads EOF and silently aborts).

Reuses `tests/vm_helpers.py` (`poll`, `NODESTATUS_ADDR`) — no helper
refactoring needed; main already extracted them.

## Error handling

- No runtime file / no registered CRN → `pytest.skip`, mirroring how
  instance and confidential tests degrade when their artifacts are absent.
- Poll failures report the last error (HTTP status/exception) via `poll`'s
  existing contract.
- Upload races with async account funding are absorbed by
  `_upload_with_balance_retry`.

## Out of scope

- `program update` / persistent programs / volumes — separate work.
- Scheduler interaction: programs are on-demand by design; nothing to assert.
- The full upstream `example_fastapi` endpoint suite (many endpoints assume
  mainnet services); the vendored app covers the message→boot→proxy path.

## Verification

- `pytest tests/test_programs.py` against a locally provisioned testnet
  (`local-up.sh` + `crn-up.sh`) passes.
- With `ALEPH_TESTNET_PROGRAM_RUNTIME` unset, the test skips cleanly.
