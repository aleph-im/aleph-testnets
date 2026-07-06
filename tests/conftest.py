import json
import os
import subprocess
import time

import pytest
import urllib.request
import urllib.error


def _require_env(name: str) -> str:
    val = os.environ.get(name)
    if not val:
        pytest.fail(f"Required environment variable {name} is not set")
    return val


@pytest.fixture(scope="session")
def ccn_url() -> str:
    return _require_env("ALEPH_TESTNET_CCN_URL").rstrip("/")


@pytest.fixture(scope="session")
def private_key() -> str:
    return _require_env("ALEPH_TESTNET_PRIVATE_KEY")


@pytest.fixture(scope="session", autouse=True)
def ccn_ready(ccn_url: str):
    """Wait for the CCN to be reachable before running any tests."""
    url = f"{ccn_url}/api/v0/version"
    deadline = time.time() + 120
    last_err = None
    while time.time() < deadline:
        try:
            req = urllib.request.urlopen(url, timeout=5)
            if req.status == 200:
                return
        except (urllib.error.URLError, OSError) as e:
            last_err = e
        time.sleep(5)
    pytest.fail(f"CCN not ready at {url} after 120s: {last_err}")


# The testnet network name. Used as the global `--network` on every CLI call,
# and, because the CLI derives the corechannel `--network-tag` from the current
# network's name, as the tag embedded in node operations (link/unlink/create).
# It MUST match:
#   - `--network testnet` in scripts/crn-up.sh (which links the CRNs), and
#   - `FILTER_TAG: testnet` on the nodestatus services (deploy/docker-compose.yml),
# otherwise nodestatus won't process these operations and CRN (un)links are
# silent no-ops.
TESTNET_NETWORK = "testnet"


@pytest.fixture(scope="session")
def aleph_cli_config(tmp_path_factory, scheduler_api_url: str) -> str:
    """Isolated CLI config dir defining a `testnet` network as the default.

    Two reasons this exists:
      1. Scheduler resolution: `aleph instance show` / `instance ssh` read the
         scheduler URL from the current network's config, so the network's
         scheduler URL points at the local testnet scheduler.
      2. Network tag: node operations embed the current network's *name* as the
         corechannel tag. Naming the network `testnet` makes link/unlink/create
         operations carry the `testnet` tag, matching crn-up.sh and the
         nodestatus FILTER_TAG.

    Returned path is exported as XDG_CONFIG_HOME by the aleph_cli fixture so the
    user's own ~/.config/aleph is never touched.
    """
    cfg = tmp_path_factory.mktemp("aleph-cli-config")
    env = {**os.environ, "XDG_CONFIG_HOME": str(cfg)}
    for cmd in (
        ["aleph", "config", "network", "add", TESTNET_NETWORK, "--scheduler-url", scheduler_api_url],
        ["aleph", "config", "network", "use", TESTNET_NETWORK],
    ):
        result = subprocess.run(cmd, env=env, capture_output=True, text=True)
        if result.returncode != 0:
            pytest.fail(f"CLI config setup failed: {' '.join(cmd)}\nStderr: {result.stderr}")
    return str(cfg)


@pytest.fixture(scope="session")
def aleph_cli(ccn_url: str, private_key: str, aleph_cli_config: str):
    """Return a function that invokes the aleph CLI with pre-configured flags.

    Every call passes `--network testnet` explicitly so the CLI can never fall
    back to its builtin mainnet network (for scheduler resolution or the
    corechannel tag). The CCN is always the raw `--ccn` URL, which takes
    precedence over the network's configured CCN.

    Usage:
        result = aleph_cli("file", "upload", "/path/to/file")
        result = aleph_cli("instance", "show", vm_hash, parse_json=True)

    With `parse_json=True`, an empty stdout (e.g. `aggregate get` on a
    missing key) yields None rather than a JSONDecodeError.
    """
    def run(*args: str, parse_json: bool = False, check: bool = True, timeout: float | None = None) -> subprocess.CompletedProcess | dict | list | None:
        cmd = ["aleph", "--ccn", ccn_url, "--network", TESTNET_NETWORK]
        if parse_json:
            cmd.append("--json")
        cmd.extend(args)
        # Signing key + isolated CLI config (for scheduler + network-tag resolution).
        env = {
            **os.environ,
            "ALEPH_PRIVATE_KEY": private_key,
            "XDG_CONFIG_HOME": aleph_cli_config,
        }
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, env=env, timeout=timeout)
        except subprocess.TimeoutExpired as e:
            # Streaming commands (e.g. `instance logs`) never exit on their own;
            # return whatever was captured so far.
            return subprocess.CompletedProcess(
                cmd, returncode=None, stdout=e.stdout or "", stderr=e.stderr or "",
            )
        if check and result.returncode != 0:
            pytest.fail(
                f"CLI command failed: {' '.join(cmd)}\n"
                f"Exit code: {result.returncode}\n"
                f"Stdout: {result.stdout}\n"
                f"Stderr: {result.stderr}"
            )
        if parse_json:
            return json.loads(result.stdout) if result.stdout.strip() else None
        return result
    return run


@pytest.fixture(scope="session")
def scheduler_api_url() -> str:
    return os.environ.get("ALEPH_TESTNET_SCHEDULER_API_URL", "http://localhost:8082")


@pytest.fixture(scope="session")
def rootfs_image() -> str:
    path = os.environ.get("ALEPH_TESTNET_ROOTFS", "")
    if not path or not os.path.exists(path):
        pytest.skip("No rootfs image: instance tests require ALEPH_TESTNET_ROOTFS")
    return path


def _upload_with_balance_retry(aleph_cli, path: str, what: str, timeout: float = 180) -> str:
    """Upload a file, retrying while the CCN reports 'Insufficient balance'.

    On a fresh testnet the account funding flows through nodestatus-balances
    asynchronously; an early big upload can race it. Real cost shortfalls
    still surface, as a failure after the timeout."""
    deadline = time.time() + timeout
    while True:
        result = aleph_cli(
            "--json", "file", "upload", path,
            "--storage-engine", "storage", "--chain", "eth",
            check=False,
        )
        if result.returncode == 0:
            item_hash = json.loads(result.stdout)["item_hash"]
            assert item_hash, f"{what} upload should return an item_hash"
            return item_hash
        if "Insufficient balance" in (result.stderr or "") and time.time() < deadline:
            time.sleep(10)
            continue
        pytest.fail(f"{what} upload failed: {(result.stderr or '').strip()[-500:]}")


@pytest.fixture(scope="session")
def rootfs_hash(aleph_cli, rootfs_image) -> str:
    """Upload the rootfs image once per session; return its item_hash.

    Instance tests reuse this instead of each re-uploading the multi-hundred-MB
    image. Retries around the funding race: this is the first big upload of a
    run and nodestatus-balances credits the account asynchronously."""
    return _upload_with_balance_retry(aleph_cli, rootfs_image, "Rootfs")


@pytest.fixture(scope="session")
def ssh_key_pair(tmp_path_factory):
    """Generate an ephemeral Ed25519 SSH key pair for instance tests."""
    key_dir = tmp_path_factory.mktemp("ssh")
    private_key = key_dir / "id_ed25519"
    public_key = key_dir / "id_ed25519.pub"
    subprocess.run(
        ["ssh-keygen", "-t", "ed25519", "-f", str(private_key), "-N", "", "-q"],
        check=True,
    )
    return str(private_key), str(public_key)


@pytest.fixture(scope="session")
def crn_ssh_key() -> str:
    """SSH private key granting root access to the CRN *hosts* (not the VMs).

    Reuses crn-up.sh's convention: the SSH_KEY_FILE environment variable,
    defaulting to ~/.ssh/id_ed25519. In CI the tests run on the CCN droplet,
    where the workflow copies the DigitalOcean key to /root/.ssh/id_ed25519
    (the same key crn-up.sh uses to install the CRNs).

    The upgrade tests need this to inspect and mutate the CRN itself:
    dpkg version checks, /etc/aleph-vm/supervisor.env edits and
    aleph-vm-supervisor restarts.
    """
    path = os.path.expanduser(os.environ.get("SSH_KEY_FILE") or "~/.ssh/id_ed25519")
    if not os.path.exists(path):
        pytest.fail(
            f"CRN host SSH key not found at {path}. The upgrade tests must run "
            "where crn-up.sh ran (the CI droplet or the operator machine); set "
            "SSH_KEY_FILE to the key that has root access to the CRN hosts."
        )
    return path
