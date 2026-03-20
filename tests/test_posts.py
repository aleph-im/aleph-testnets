import time

import pytest


def test_post_create_and_list(aleph_cli, unique_channel):
    """Create a post, list posts by channel, verify it appears."""
    content = '{"body": "hello from integration test"}'
    result = aleph_cli(
        "post", "create",
        "--type", "test-post",
        "--content", content,
        "--channel", unique_channel,
        parse_json=True,
    )
    item_hash = result["item_hash"]
    assert item_hash

    # Give the CCN a moment to process
    time.sleep(2)

    posts = aleph_cli(
        "post", "list",
        "--channels", unique_channel,
        parse_json=True,
    )
    hashes = [p["item_hash"] for p in posts]
    assert item_hash in hashes, f"Created post {item_hash} should appear in channel listing"


@pytest.mark.xfail(reason="CLI bug: amend doesn't include 'type' field in content, CCN rejects with InvalidMessageFormat")
def test_post_amend(aleph_cli, unique_channel):
    """Create a post, amend it, verify updated content."""
    original = '{"body": "original"}'
    result = aleph_cli(
        "post", "create",
        "--type", "test-post",
        "--content", original,
        "--channel", unique_channel,
        parse_json=True,
    )
    item_hash = result["item_hash"]

    time.sleep(2)

    updated = '{"body": "amended"}'
    aleph_cli(
        "post", "amend",
        "--ref", item_hash,
        "--content", updated,
        "--channel", unique_channel,
        parse_json=True,
    )

    time.sleep(2)

    posts = aleph_cli(
        "post", "list",
        "--channels", unique_channel,
        parse_json=True,
    )
    # Find the post and check its content was amended
    matching = [p for p in posts if p.get("original_item_hash") == item_hash or p["item_hash"] == item_hash]
    assert len(matching) > 0, "Amended post should appear in listing"
    latest = matching[0]
    assert latest["content"]["body"] == "amended", "Post content should reflect the amendment"
