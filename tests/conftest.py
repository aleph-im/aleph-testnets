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
