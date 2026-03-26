import time

import pytest


# Anvil account #1 — primary test account (see scripts/fund-test-accounts.sh)
TEST_ADDR = "0x70997970C51812dc3A010C7d01b50e0d17dc79C8"

TRANSFERS_QUERY = """
query GetTransfers($blockchain: String!, $token: String!, $limit: Int) {
    transfers(blockchain: $blockchain, token: $token, limit: $limit) {
        from
        to
        value
        transaction
        timestamp
    }
}
"""


@pytest.fixture(scope="module")
def usdaleph_addr(contracts):
    return contracts["usdaleph"]


@pytest.fixture(scope="module")
def credit_contract_addr(contracts):
    return contracts["credit_contract"]


# -- Sanity checks ----------------------------------------------------------

def test_account_prefunded(usdaleph_addr, cast_call):
    """Sanity check: the test account was funded with USDAleph by the setup script."""
    raw = cast_call(usdaleph_addr, "balanceOf(address)(uint256)", TEST_ADDR)
    balance = int(raw.split()[0])
    # fund-test-accounts.sh mints 200 USDAleph and sends 100 to the credit contract,
    # leaving ~100 USDAleph. Previous test runs may have spent some.
    min_required = 10_000_000  # 10 USDAleph — enough for all credit tests
    assert balance >= min_required, (
        f"Test account needs at least {min_required} raw USDAleph, has {balance}. "
        f"Re-run fund-test-accounts.sh or reset the dev env."
    )


# -- Indexer tests -----------------------------------------------------------

def test_credit_transfer_indexed(
    usdaleph_addr,
    credit_contract_addr,
    cast_send,
    cast_call,
    indexer_graphql,
):
    """Send USDAleph to the credit contract, verify the indexer picks it up."""
    transfer_amount = "1000000"  # 1 USDAleph (6 decimals)

    # Verify test account has balance
    raw_balance = cast_call(usdaleph_addr, "balanceOf(address)(uint256)", TEST_ADDR)
    balance = raw_balance.split()[0]  # strip cast's annotation e.g. "10000000000 [1e10]"
    assert int(balance) >= int(transfer_amount), (
        f"Test account needs at least {transfer_amount} USDAleph, has {balance}"
    )

    # Transfer USDAleph to credit contract
    cast_send(
        usdaleph_addr,
        "transfer(address,uint256)",
        credit_contract_addr,
        transfer_amount,
    )

    # Poll the indexer until the transfer appears (up to 5 min)
    deadline = time.time() + 300
    transfers = []
    while time.time() < deadline:
        try:
            result = indexer_graphql(
                TRANSFERS_QUERY,
                {
                    "blockchain": "ethereum",
                    "token": "USDC",
                    "limit": 100,
                },
            )
            transfers = result.get("data", {}).get("transfers", [])
            matching = [
                t for t in transfers
                if t["from"].lower() == TEST_ADDR.lower()
            ]
            if matching:
                transfer = matching[0]
                assert transfer["to"].lower() == credit_contract_addr.lower()
                return  # Pass!
        except Exception:
            pass  # Indexer may still be starting up
        time.sleep(3)

    pytest.fail(
        f"Transfer from {TEST_ADDR} not found in indexer after 300s. "
        f"Got {len(transfers)} transfers total."
    )


def test_credit_multiple_transfers(
    usdaleph_addr,
    credit_contract_addr,
    cast_send,
    indexer_graphql,
):
    """Send multiple transfers and verify all appear in the indexer."""
    amounts = ["500000", "750000", "250000"]  # 0.5, 0.75, 0.25 USDAleph

    # Count existing transfers before sending new ones
    try:
        result = indexer_graphql(
            TRANSFERS_QUERY,
            {"blockchain": "ethereum", "token": "USDC", "limit": 100},
        )
        initial_count = len([
            t for t in result.get("data", {}).get("transfers", [])
            if t["from"].lower() == TEST_ADDR.lower()
        ])
    except Exception:
        initial_count = 0

    for amount in amounts:
        cast_send(
            usdaleph_addr,
            "transfer(address,uint256)",
            credit_contract_addr,
            amount,
        )

    # Poll the indexer until all 3 NEW transfers appear (up to 5 min)
    expected = initial_count + 3
    deadline = time.time() + 300
    from_test = []
    while time.time() < deadline:
        try:
            result = indexer_graphql(
                TRANSFERS_QUERY,
                {
                    "blockchain": "ethereum",
                    "token": "USDC",
                    "limit": 100,
                },
            )
            transfers = result.get("data", {}).get("transfers", [])
            from_test = [
                t for t in transfers
                if t["from"].lower() == TEST_ADDR.lower()
            ]
            if len(from_test) >= expected:
                return  # Pass!
        except Exception:
            pass  # Indexer may still be catching up
        time.sleep(3)

    pytest.fail(
        f"Expected at least {expected} transfers from {TEST_ADDR}, "
        f"found {len(from_test)} after 300s"
    )


# -- Full credit lifecycle ---------------------------------------------------

def test_credit_lifecycle(
    usdaleph_addr,
    credit_contract_addr,
    cast_send,
    ccn_api,
    indexer_graphql,
):
    """Full lifecycle: fund credits via USDAleph transfer, verify credit balance appears on CCN.

    Flow: ERC20 transfer -> indexer -> credit-service -> CCN distribution POST -> credit balance
    """
    transfer_amount = "2000000"  # 2 USDAleph (6 decimals) = 2,000,000 credits expected

    # Record the initial credit balance (may be non-zero from prior tests)
    try:
        initial = ccn_api(f"/api/v0/addresses/{TEST_ADDR}/balance")
        initial_credits = initial.get("credit_balance", 0)
    except Exception:
        initial_credits = 0

    # Send USDAleph to the credit contract
    cast_send(
        usdaleph_addr,
        "transfer(address,uint256)",
        credit_contract_addr,
        transfer_amount,
    )

    # Wait for the indexer to pick up the transfer first (sanity gate)
    deadline = time.time() + 120
    indexed = False
    while time.time() < deadline:
        try:
            result = indexer_graphql(
                TRANSFERS_QUERY,
                {"blockchain": "ethereum", "token": "USDC", "limit": 100},
            )
            transfers = result.get("data", {}).get("transfers", [])
            matching = [
                t for t in transfers
                if t["from"].lower() == TEST_ADDR.lower()
                and t["value"] == transfer_amount
            ]
            if matching:
                indexed = True
                break
        except Exception:
            pass
        time.sleep(3)

    if not indexed:
        pytest.fail("Transfer not indexed after 120s — credit-service won't see it")

    # Now wait for the credit balance to increase on the CCN.
    # The credit-service polls every 10s, then publishes a distribution POST
    # to the CCN which processes it and updates the balance.
    expected_increase = 2_000_000  # 2 USDC * 1M credits/USDC
    deadline = time.time() + 120
    last_balance = initial_credits

    while time.time() < deadline:
        try:
            resp = ccn_api(f"/api/v0/addresses/{TEST_ADDR}/balance")
            last_balance = resp.get("credit_balance", 0)
            if last_balance >= initial_credits + expected_increase:
                return  # Pass!
        except Exception:
            pass
        time.sleep(5)

    pytest.fail(
        f"Credit balance did not increase as expected after 120s. "
        f"Initial: {initial_credits}, current: {last_balance}, "
        f"expected at least: {initial_credits + expected_increase}"
    )
