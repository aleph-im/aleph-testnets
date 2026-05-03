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


@pytest.fixture(scope="session")
def aleph_cli(ccn_url: str, private_key: str):
    """Return a function that invokes the aleph CLI with pre-configured flags.

    Usage:
        result = aleph_cli("file", "upload", "/path/to/file")
        result = aleph_cli("post", "list", "--channels", "test", parse_json=True)
    """
    def run(*args: str, parse_json: bool = False, check: bool = True) -> subprocess.CompletedProcess | dict:
        cmd = ["aleph", "--ccn-url", ccn_url]
        if parse_json:
            cmd.append("--json")
        cmd.extend(args)
        # For commands that need signing, inject the private key via env var
        env = {**os.environ, "ALEPH_PRIVATE_KEY": private_key}
        result = subprocess.run(cmd, capture_output=True, text=True, env=env)
        if check and result.returncode != 0:
            pytest.fail(
                f"CLI command failed: {' '.join(cmd)}\n"
                f"Exit code: {result.returncode}\n"
                f"Stdout: {result.stdout}\n"
                f"Stderr: {result.stderr}"
            )
        if parse_json:
            return json.loads(result.stdout)
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
def ccn_aggregates(ccn_url: str):
    """Return a function that fetches an aggregate from the CCN."""
    def get(address: str, key: str) -> dict | None:
        url = f"{ccn_url}/api/v0/aggregates/{address}.json?keys={key}"
        req = urllib.request.Request(url, headers={"Accept": "application/json"})
        try:
            resp = urllib.request.urlopen(req, timeout=10)
            data = json.loads(resp.read())
            return data.get("data", {}).get(key)
        except urllib.error.HTTPError:
            return None
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
def crn_nodes(ccn_aggregates):
    """Registered CRN entries from the corechannel aggregate.

    Returns a list of dicts, each with at least 'hash' and 'address' keys.
    Requires CRN_COUNT=2 (or more) during provisioning.
    """
    NODESTATUS_ADDR = "0x70997970C51812dc3A010C7d01b50e0d17dc79C8"
    agg = ccn_aggregates(NODESTATUS_ADDR, "corechannel")
    if agg is None:
        pytest.skip("No corechannel aggregate — migration tests require registered CRNs")
    resource_nodes = agg.get("resource_nodes", [])
    if len(resource_nodes) < 2:
        pytest.skip(
            f"Need at least 2 CRNs for migration tests, found {len(resource_nodes)}"
        )
    return resource_nodes
