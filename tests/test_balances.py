"""Tests for ALEPH balance tracking via nodestatus-balances.

The nodestatus-balances container monitors MockALEPH Transfer events on Anvil
and publishes balances-update posts to the CCN.
"""
import time

import pytest

# Anvil account #4 — primary test account
TEST_ADDR = "0x15d34AAf54267DB7D7c367839AAf71A00a2C6A65"

# Nodestatus signing account (#1) — publishes balances-update posts
NODESTATUS_ADDR = "0x70997970C51812dc3A010C7d01b50e0d17dc79C8"


def test_aleph_balance_funded(mock_aleph_addr, cast_call):
    """Sanity check: test account was funded with MockALEPH by the setup script."""
    raw = cast_call(mock_aleph_addr, "balanceOf(address)(uint256)", TEST_ADDR)
    balance = int(raw.split()[0])
    # fund-test-accounts.sh mints 1,000,000 ALEPH (18 decimals)
    min_required = 100_000 * 10**18  # at least 100K ALEPH
    assert balance >= min_required, (
        f"Test account needs at least 100K ALEPH, has {balance / 10**18:.0f}"
    )


def test_balances_update_posted(ccn_messages):
    """Verify nodestatus-balances has published at least one balances-update post."""
    deadline = time.time() + 120
    while time.time() < deadline:
        messages = ccn_messages({
            "msgType": "POST",
            "contentTypes": "balances-update",
            "addresses": NODESTATUS_ADDR,
            "pagination": 1,
        })
        if messages:
            msg = messages[0]
            content = msg.get("content", {}).get("content", {})
            assert "balances" in content, "balances-update post missing 'balances' field"
            assert "height" in content, "balances-update post missing 'height' field"
            return
        time.sleep(5)

    pytest.fail("No balances-update post from nodestatus-balances after 120s")


def test_aleph_balance_transfer(mock_aleph_addr, cast_send, ccn_messages):
    """Transfer MockALEPH on Anvil, verify the balance update is published to CCN."""
    # Anvil account #5
    recipient = "0x9965507D1a55bcC2695C58ba16FB37d819B0A4dc"
    transfer_amount = str(1000 * 10**18)  # 1000 ALEPH

    # Record current balances-update count
    initial_messages = ccn_messages({
        "msgType": "POST",
        "contentTypes": "balances-update",
        "addresses": NODESTATUS_ADDR,
        "pagination": 100,
    })
    initial_count = len(initial_messages)

    # Transfer MockALEPH
    cast_send(
        mock_aleph_addr,
        "transfer(address,uint256)",
        recipient,
        transfer_amount,
    )

    # Wait for a NEW balances-update post (monitor-balances polls every 5s)
    deadline = time.time() + 120
    while time.time() < deadline:
        messages = ccn_messages({
            "msgType": "POST",
            "contentTypes": "balances-update",
            "addresses": NODESTATUS_ADDR,
            "pagination": 100,
        })
        if len(messages) > initial_count:
            # Check the latest post includes the recipient
            latest = messages[0]
            balances = latest.get("content", {}).get("content", {}).get("balances", {})
            if recipient.lower() in {k.lower() for k in balances}:
                return
        time.sleep(5)

    pytest.fail(
        f"No new balances-update post containing {recipient} after 120s. "
        f"Had {initial_count} posts initially."
    )
