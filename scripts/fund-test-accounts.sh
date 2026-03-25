#!/usr/bin/env bash
# Mint USDAleph tokens to test accounts.
# Accepts compose file args via COMPOSE_FILES_STR env var (space-separated)
# or defaults to deploy/docker-compose.yml.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
DEPLOY_DIR="$REPO_ROOT/deploy"
LOCAL_DIR="$REPO_ROOT/.local"
CONTRACTS_JSON="$LOCAL_DIR/contracts.json"

# Anvil account 0 — deployer / token owner
DEPLOYER_KEY="0xac0974bec39a17e36ba4a6b4d238ff944bacb478cbed5efcae784d7bf4f2ff80"

# Test account (derived from private key aaaa...aa)
TEST_ADDR="0x8fd379246834eac74B8419FfdA202CF8051F7A03"

# Amount to mint: 10,000 USDAleph (6 decimals)
MINT_AMOUNT="10000000000" # 10_000 * 10^6

if [ ! -f "$CONTRACTS_JSON" ]; then
    echo "ERROR: $CONTRACTS_JSON not found. Run deploy-contracts.sh first."
    exit 1
fi

USDALEPH_ADDR=$(jq -r '.usdaleph' "$CONTRACTS_JSON")

# Parse compose files from env var (space-separated string → array)
if [ -n "${COMPOSE_FILES_STR:-}" ]; then
    read -ra COMPOSE_FILES <<< "$COMPOSE_FILES_STR"
else
    COMPOSE_FILES=(-f "$DEPLOY_DIR/docker-compose.yml")
fi

# Helper: run cast inside the Foundry container
cast_run() {
    docker compose "${COMPOSE_FILES[@]}" exec \
        anvil cast "$@"
}

echo "==> Sending 1 ETH to $TEST_ADDR for gas..."
cast_run send \
    --rpc-url http://anvil:8545 \
    --private-key "$DEPLOYER_KEY" \
    --value "1ether" \
    "$TEST_ADDR"

echo "==> Minting $MINT_AMOUNT USDAleph (raw) to $TEST_ADDR..."
cast_run send \
    --rpc-url http://anvil:8545 \
    --private-key "$DEPLOYER_KEY" \
    "$USDALEPH_ADDR" \
    "mint(address,uint256)" \
    "$TEST_ADDR" \
    "$MINT_AMOUNT"

echo "==> Verifying balance..."
BALANCE=$(cast_run call \
    --rpc-url http://anvil:8545 \
    "$USDALEPH_ADDR" \
    "balanceOf(address)(uint256)" \
    "$TEST_ADDR")
echo "    Test account balance: $BALANCE"

echo "==> Fund complete!"
