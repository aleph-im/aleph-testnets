import time
import uuid

import pytest

# Anvil account #1 — primary test account (see scripts/fund-test-accounts.sh)
TEST_ADDR = "0x70997970C51812dc3A010C7d01b50e0d17dc79C8"


def _download_with_retry(aleph_cli, download_args, timeout=30):
    """Retry file download until available (CCN needs time to process the STORE message)."""
    deadline = time.time() + timeout
    while True:
        result = aleph_cli(*download_args, check=False)
        if result.returncode == 0:
            return
        if time.time() >= deadline:
            pytest.fail(
                f"File download failed after {timeout}s\n"
                f"Stdout: {result.stdout}\n"
                f"Stderr: {result.stderr}"
            )
        time.sleep(2)


def test_file_upload_and_download(aleph_cli, tmp_file, tmp_path):
    """Upload a file and download it by hash, compare bytes."""
    result = aleph_cli("file", "upload", str(tmp_file), parse_json=True)
    message_hash = result["item_hash"]
    assert message_hash, "Upload should return an item_hash"

    out = tmp_path / "downloaded.bin"
    _download_with_retry(aleph_cli, ("file", "download", "--message-hash", message_hash, "--output", str(out)))
    assert out.read_bytes() == tmp_file.read_bytes(), "Downloaded file should match uploaded file"


def test_file_upload_with_ref(aleph_cli, tmp_file, tmp_path):
    """Upload a file with a user-defined ref, download by ref."""
    ref = f"test-ref-{uuid.uuid4().hex[:8]}"
    result = aleph_cli("file", "upload", str(tmp_file), "--ref", ref, parse_json=True)
    assert result["item_hash"], "Upload should return an item_hash"

    out = tmp_path / "downloaded_ref.bin"
    _download_with_retry(aleph_cli, ("file", "download", "--ref", ref, "--owner", TEST_ADDR, "--output", str(out)))
    assert out.read_bytes() == tmp_file.read_bytes(), "Downloaded file should match uploaded file"
