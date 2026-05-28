import json
import os
import subprocess
import time
import uuid

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
# and — because the CLI derives the corechannel `--network-tag` from the current
# network's name — as the tag embedded in node operations (link/unlink/create).
# It MUST match:
#   - `--network testnet` in scripts/crn-up.sh (which links the CRNs), and
#   - `FILTER_TAG: testnet` on the nodestatus services (deploy/docker-compose.yml),
# otherwise nodestatus won't process these operations and CRN (un)links are
# silent no-ops (see test_migration).
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
        result = aleph_cli("post", "list", "--channels", "test", parse_json=True)

    With `parse_json=True`, an empty stdout (e.g. `aggregate get` on a
    missing key) yields None rather than a JSONDecodeError.
    """
    def run(*args: str, parse_json: bool = False, check: bool = True) -> subprocess.CompletedProcess | dict | list | None:
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
        result = subprocess.run(cmd, capture_output=True, text=True, env=env)
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


@pytest.fixture
def unique_channel() -> str:
    """Generate a unique channel name to avoid test collisions."""
    return f"test-{uuid.uuid4().hex[:12]}"


@pytest.fixture
def tmp_file(tmp_path):
    """Create a temporary file with random content for upload tests."""
    f = tmp_path / "testfile.bin"
    f.write_bytes(os.urandom(1024))
    return f


@pytest.fixture(scope="session")
def ccn_api(ccn_url: str):
    """Return a function that queries the CCN REST API and returns parsed JSON."""
    def get(path: str) -> dict:
        url = f"{ccn_url}{path}"
        req = urllib.request.Request(url, headers={"Accept": "application/json"})
        resp = urllib.request.urlopen(req, timeout=10)
        return json.loads(resp.read())
    return get


@pytest.fixture(scope="session")
def ccn_messages(ccn_url: str):
    """Return a function that queries the CCN messages API."""
    def query(params: dict) -> list:
        qs = "&".join(f"{k}={v}" for k, v in params.items())
        url = f"{ccn_url}/api/v0/messages.json?{qs}"
        req = urllib.request.Request(url, headers={"Accept": "application/json"})
        resp = urllib.request.urlopen(req, timeout=10)
        data = json.loads(resp.read())
        return data.get("messages", [])
    return query


@pytest.fixture(scope="session")
def ccn_aggregates(aleph_cli):
    """Return a function that fetches an aggregate value via `aleph aggregate get`.

    Returns the unwrapped value at `key` (matching the urllib-based fixture's
    contract). The CLI emits `{"<key>": <value>}` — we drop the wrapping.
    Returns None if the aggregate does not exist (the CLI exits 0 with empty
    stdout in that case).
    """
    def get(address: str, key: str) -> dict | None:
        raw = aleph_cli("aggregate", "get", key, "--address", address, parse_json=True)
        if not isinstance(raw, dict):
            return None
        return raw.get(key)
    return get


@pytest.fixture(scope="session")
def contracts():
    """Load deployed contract addresses from .local/contracts.json."""
    path = os.environ.get("ALEPH_TESTNET_CONTRACTS_JSON", "")
    if not path or not os.path.exists(path):
        pytest.skip("No contracts.json — credits tests require deployed contracts")
    with open(path) as f:
        return json.load(f)


@pytest.fixture(scope="session")
def mock_aleph_addr(contracts):
    """MockALEPH contract address on Anvil."""
    return contracts["mock_aleph"]


@pytest.fixture(scope="session")
def mint_aleph(mock_aleph_addr, cast_send):
    """Return a function that mints MockALEPH to an address.

    Amount is in whole ALEPH tokens (e.g. 1000000 = 1M ALEPH).
    MockALEPH has 18 decimals.
    """
    deployer_key = "0xac0974bec39a17e36ba4a6b4d238ff944bacb478cbed5efcae784d7bf4f2ff80"

    def mint(to: str, amount: int):
        raw = str(amount * 10**18)
        cast_send(
            mock_aleph_addr,
            "mint(address,uint256)",
            to, raw,
            private_key=deployer_key,
        )
    return mint


@pytest.fixture(scope="session")
def indexer_url() -> str:
    return os.environ.get("ALEPH_TESTNET_INDEXER_URL", "http://localhost:8081")


@pytest.fixture(scope="session")
def anvil_rpc() -> str:
    return os.environ.get("ALEPH_TESTNET_ANVIL_RPC", "http://localhost:8545")


@pytest.fixture(scope="session")
def scheduler_api_url() -> str:
    return os.environ.get("ALEPH_TESTNET_SCHEDULER_API_URL", "http://localhost:8082")


@pytest.fixture(scope="session")
def rootfs_image() -> str:
    path = os.environ.get("ALEPH_TESTNET_ROOTFS", "")
    if not path or not os.path.exists(path):
        pytest.skip("No rootfs image — instance tests require ALEPH_TESTNET_ROOTFS")
    return path


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
def indexer_graphql(indexer_url: str):
    """Return a function that queries the indexer's GraphQL endpoint."""
    def query(graphql_query: str, variables: dict | None = None) -> dict:
        payload = json.dumps({"query": graphql_query, "variables": variables or {}})
        req = urllib.request.Request(
            f"{indexer_url}/graphql",
            data=payload.encode(),
            headers={"Content-Type": "application/json"},
        )
        resp = urllib.request.urlopen(req, timeout=10)
        return json.loads(resp.read())
    return query


@pytest.fixture(scope="session")
def cast_send(anvil_rpc: str):
    """Return a function that runs cast send against Anvil."""
    def send(
        to: str,
        sig: str,
        *args: str,
        private_key: str = "0x47e179ec197488593b187f80a00eb0da91f1b9d0b13f8733639f19c30a34926a",  # Anvil #4
        value: str | None = None,
    ) -> subprocess.CompletedProcess:
        cmd = [
            "cast", "send",
            "--rpc-url", anvil_rpc,
            "--private-key", private_key,
            to, sig, *args,
        ]
        if value:
            cmd.extend(["--value", value])
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode != 0:
            pytest.fail(
                f"cast send failed: {' '.join(cmd)}\n"
                f"Stdout: {result.stdout}\n"
                f"Stderr: {result.stderr}"
            )
        return result
    return send


@pytest.fixture(scope="session")
def cast_call(anvil_rpc: str):
    """Return a function that runs cast call (read-only) against Anvil."""
    def call(to: str, sig: str, *args: str) -> str:
        cmd = [
            "cast", "call",
            "--rpc-url", anvil_rpc,
            to, sig, *args,
        ]
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode != 0:
            pytest.fail(
                f"cast call failed: {' '.join(cmd)}\n"
                f"Stdout: {result.stdout}\n"
                f"Stderr: {result.stderr}"
            )
        return result.stdout.strip()
    return call


@pytest.fixture(scope="session")
def crn_nodes(aleph_cli):
    """Registered CRN entries from the testnet's corechannel aggregate.

    Returns a list of dicts, each with at least 'hash' and 'address' keys
    (the `address` field is the CRN's HTTP endpoint URL). Requires CRN_COUNT=2
    or more during provisioning.
    """
    NODESTATUS_ADDR = "0x70997970C51812dc3A010C7d01b50e0d17dc79C8"
    nodes = aleph_cli(
        "node", "list",
        "--type", "crn",
        "--all",
        "--corechannel-address", NODESTATUS_ADDR,
        parse_json=True,
    )
    if not nodes:
        pytest.skip("No CRNs registered — migration tests require registered CRNs")
    if len(nodes) < 2:
        pytest.skip(f"Need at least 2 CRNs for migration tests, found {len(nodes)}")
    return nodes
