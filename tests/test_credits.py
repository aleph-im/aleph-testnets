import time

import pytest


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


def test_credit_transfer_indexed(
    usdaleph_addr,
    credit_contract_addr,
    cast_send,
    cast_call,
    indexer_graphql,
):
    """Send USDAleph to the credit contract, verify the indexer picks it up."""
    test_addr = "0x8fd379246834eac74B8419FfdA202CF8051F7A03"
    transfer_amount = "1000000"  # 1 USDAleph (6 decimals)

    # Verify test account has balance
    raw_balance = cast_call(usdaleph_addr, "balanceOf(address)(uint256)", test_addr)
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
                if t["from"].lower() == test_addr.lower()
            ]
            if matching:
                transfer = matching[0]
                assert transfer["to"].lower() == credit_contract_addr.lower()
                return  # Pass!
        except Exception:
            pass  # Indexer may still be starting up
        time.sleep(3)

    pytest.fail(
        f"Transfer from {test_addr} not found in indexer after 300s. "
        f"Got {len(transfers)} transfers total."
    )


def test_credit_multiple_transfers(
    usdaleph_addr,
    credit_contract_addr,
    cast_send,
    indexer_graphql,
):
    """Send multiple transfers and verify all appear in the indexer."""
    test_addr = "0x8fd379246834eac74B8419FfdA202CF8051F7A03"
    amounts = ["500000", "750000", "250000"]  # 0.5, 0.75, 0.25 USDAleph

    # Count existing transfers before sending new ones
    try:
        result = indexer_graphql(
            TRANSFERS_QUERY,
            {"blockchain": "ethereum", "token": "USDC", "limit": 100},
        )
        initial_count = len([
            t for t in result.get("data", {}).get("transfers", [])
            if t["from"].lower() == test_addr.lower()
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
                if t["from"].lower() == test_addr.lower()
            ]
            if len(from_test) >= expected:
                return  # Pass!
        except Exception:
            pass  # Indexer may still be catching up
        time.sleep(3)

    pytest.fail(
        f"Expected at least {expected} transfers from {test_addr}, "
        f"found {len(from_test)} after 300s"
    )
