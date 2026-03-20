import time
import uuid


def test_aggregate_create_and_read(aleph_cli):
    """Create an aggregate with a unique key, read it back, verify content."""
    key = f"test-key-{uuid.uuid4().hex[:8]}"
    content = '{"score": 42, "name": "integration-test"}'
    aleph_cli(
        "aggregate", "create",
        "--key", key,
        "--content", content,
        parse_json=True,
    )

    time.sleep(2)

    # Read aggregate back via the CCN API using message list
    # (the CLI doesn't have a dedicated aggregate read, so we list messages)
    messages = aleph_cli(
        "message", "list",
        "--message-types", "aggregate",
        parse_json=True,
    )
    # Verify our aggregate key appears in the results
    found = False
    for msg in messages:
        if msg.get("content", {}).get("key") == key:
            found = True
            assert msg["content"]["content"]["score"] == 42
            assert msg["content"]["content"]["name"] == "integration-test"
            break
    assert found, f"Aggregate with key '{key}' should appear in message listing"
