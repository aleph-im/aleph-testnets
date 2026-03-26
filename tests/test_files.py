import time
import uuid

import pytest

# Anvil account #1 — primary test account (see scripts/fund-test-accounts.sh)
TEST_ADDR = "0x70997970C51812dc3A010C7d01b50e0d17dc79C8"


def test_file_upload_and_download(aleph_cli, tmp_file, tmp_path):
    """Upload a file and download it by hash, compare bytes."""
    result = aleph_cli("file", "upload", str(tmp_file), parse_json=True)
    file_hash = result["item_hash"]
    assert file_hash, "Upload should return an item_hash"

    time.sleep(2)

    out = tmp_path / "downloaded.bin"
    aleph_cli("file", "download", file_hash, "--output", str(out))
    assert out.read_bytes() == tmp_file.read_bytes(), "Downloaded file should match uploaded file"


def test_file_upload_with_ref(aleph_cli, tmp_file, tmp_path):
    """Upload a file with a user-defined ref, download by ref."""
    ref = f"test-ref-{uuid.uuid4().hex[:8]}"
    result = aleph_cli("file", "upload", str(tmp_file), "--ref", ref, parse_json=True)
    assert result["item_hash"], "Upload should return an item_hash"

    time.sleep(2)

    out = tmp_path / "downloaded_ref.bin"
    aleph_cli("file", "download", "--ref", ref, "--owner", TEST_ADDR, "--output", str(out))
    assert out.read_bytes() == tmp_file.read_bytes(), "Downloaded file should match uploaded file"
