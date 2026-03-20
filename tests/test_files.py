import time
import uuid

import pytest


@pytest.mark.xfail(reason="STORE messages rejected by CCN — needs investigation")
def test_file_upload_and_download(aleph_cli, tmp_file, tmp_path):
    """Upload a file and download it by hash, compare bytes."""
    result = aleph_cli("file", "upload", str(tmp_file), parse_json=True)
    file_hash = result["item_hash"]
    assert file_hash, "Upload should return an item_hash"

    time.sleep(2)

    out = tmp_path / "downloaded.bin"
    aleph_cli("file", "download", "--hash", file_hash, "--output", str(out))
    assert out.read_bytes() == tmp_file.read_bytes(), "Downloaded file should match uploaded file"


@pytest.mark.xfail(reason="STORE messages rejected by CCN — needs investigation")
def test_file_upload_with_ref(aleph_cli, tmp_file, tmp_path):
    """Upload a file with a user-defined ref, download by ref."""
    ref = f"test-ref-{uuid.uuid4().hex[:8]}"
    result = aleph_cli("file", "upload", str(tmp_file), "--ref", ref, parse_json=True)
    assert result["item_hash"], "Upload should return an item_hash"

    time.sleep(2)

    out = tmp_path / "downloaded_ref.bin"
    aleph_cli("file", "download", "--ref", ref, "--output", str(out))
    assert out.read_bytes() == tmp_file.read_bytes(), "Downloaded file should match uploaded file"
