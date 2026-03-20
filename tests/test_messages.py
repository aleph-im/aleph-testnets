import time


def test_message_list(aleph_cli, unique_channel):
    """Create a message and verify it appears in the message listing."""
    aleph_cli(
        "post", "create",
        "--type", "test-msg",
        "--content", '{"purpose": "list-test"}',
        "--channel", unique_channel,
        parse_json=True,
    )

    time.sleep(2)

    messages = aleph_cli(
        "message", "list",
        "--channels", unique_channel,
        parse_json=True,
    )
    assert len(messages) > 0, "Should find at least one message in the channel"
    assert messages[0].get("channel") == unique_channel


def test_message_forget(aleph_cli, unique_channel):
    """Create a post, forget it, verify it disappears."""
    result = aleph_cli(
        "post", "create",
        "--type", "test-forget",
        "--content", '{"ephemeral": true}',
        "--channel", unique_channel,
        parse_json=True,
    )
    item_hash = result["item_hash"]

    time.sleep(2)

    # Forget the message
    aleph_cli("message", "forget", item_hash, parse_json=True)

    time.sleep(2)

    # Verify it's gone from listings
    messages = aleph_cli(
        "message", "list",
        "--channels", unique_channel,
        parse_json=True,
    )
    remaining_hashes = [m["item_hash"] for m in messages]
    assert item_hash not in remaining_hashes, "Forgotten message should not appear in listing"
