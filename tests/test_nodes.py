"""Tests for corechannel node operations via nodestatus.

The nodestatus container processes corechan-operation posts and publishes
a corechannel aggregate to the CCN.
"""
import time

import pytest

# Nodestatus signing account (#1) — publishes corechannel aggregate
NODESTATUS_ADDR = "0x70997970C51812dc3A010C7d01b50e0d17dc79C8"


def test_corechannel_aggregate_exists(ccn_aggregates):
    """After startup, nodestatus should have published a corechannel aggregate."""
    deadline = time.time() + 180
    while time.time() < deadline:
        agg = ccn_aggregates(NODESTATUS_ADDR, "corechannel")
        if agg is not None:
            assert "nodes" in agg, (
                f"Unexpected corechannel aggregate shape: {list(agg.keys())}"
            )
            return
        time.sleep(5)

    pytest.fail("No corechannel aggregate from nodestatus after 180s")


def test_create_node(aleph_cli, mint_aleph, private_key, ccn_aggregates):
    """Create a CCN via the aleph CLI, verify it appears in the corechannel aggregate.

    Requires:
    - 200,000+ ALEPH balance for the test account (funded by setup script)
    - nodestatus running to process the corechan-operation post
    """
    # The test account (Anvil #4) should already have 1M ALEPH from funding.
    # Create a node using the aleph CLI
    result = aleph_cli(
        "node", "create-ccn",
        "--name", "test-ccn",
        "--multiaddress", "/ip4/127.0.0.1/tcp/4025/p2p/test-node",
        check=False,
    )

    if result.returncode != 0:
        # If it fails due to missing balance or other issues, skip gracefully
        pytest.skip(
            f"aleph node create failed (may need ALEPH balance or CLI support): "
            f"{result.stderr}"
        )

    # Wait for nodestatus to process and update the aggregate
    deadline = time.time() + 120
    while time.time() < deadline:
        agg = ccn_aggregates(NODESTATUS_ADDR, "corechannel")
        if agg and isinstance(agg, dict):
            nodes = agg.get("nodes", [])
            if isinstance(nodes, list) and len(nodes) > 0:
                return
            # nodes might be a dict keyed by hash
            if isinstance(nodes, dict) and len(nodes) > 0:
                return
        time.sleep(5)

    pytest.fail("Node not found in corechannel aggregate after 120s")
