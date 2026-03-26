#!/usr/bin/env bash
# Fund test accounts with ETH, USDAleph tokens, and Aleph credits.
#
# Anvil account layout:
#   #0  0xf39F...  Reserved — contract deployer + credit service sender
#   #1  0x7099...  Test account
#   #2  0x3C44...  Test account
#   #3  0x90F7...  Test account
#
# Each test account receives:
#   - 1 ETH (gas)
#   - 200 USDAleph (100 kept as tokens, 100 sent to credit contract → 100M credits)
#
# Accepts compose file args via COMPOSE_FILES_STR env var (space-separated).
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
DEPLOY_DIR="$REPO_ROOT/deploy"
LOCAL_DIR="$REPO_ROOT/.local"
CONTRACTS_JSON="$LOCAL_DIR/contracts.json"
CCN_URL="http://localhost:4024"

# Anvil account #0 — privileged (deployer + credit service)
DEPLOYER_KEY="0xac0974bec39a17e36ba4a6b4d238ff944bacb478cbed5efcae784d7bf4f2ff80"
DEPLOYER_ADDR="0xf39Fd6e51aad88F6F4ce6aB8827279cffFb92266"

# Test accounts: Anvil #1, #2, #3
TEST_ADDRS=(
    "0x70997970C51812dc3A010C7d01b50e0d17dc79C8"
    "0x3C44CdDdB6a900fa2b585dd299e03d12FA4293BC"
    "0x90F79bf6EB2c4f870365E785982E1f101E93b906"
)
TEST_KEYS=(
    "0x59c6995e998f97a5a0044966f0945389dc9e86dae88c7a8412f4603b6b78690d"
    "0x5de4111afa1a4b94908f83103eb1f1706367c2e68ca870fc3fb9a804cdab365a"
    "0x7c852118294e51e653712a81e05800f419141751be58f605c371e15141b007a6"
)

# Amounts (USDAleph has 6 decimals, 1 USDAleph = 1,000,000 raw)
MINT_AMOUNT="200000000"      # 200 USDAleph per account
CREDIT_AMOUNT="100000000"    # 100 USDAleph → credit contract → 100,000,000 credits

if [ ! -f "$CONTRACTS_JSON" ]; then
    echo "ERROR: $CONTRACTS_JSON not found. Run deploy-contracts.sh first."
    exit 1
fi

USDALEPH_ADDR=$(jq -r '.usdaleph' "$CONTRACTS_JSON")
CREDIT_ADDR=$(jq -r '.credit_contract' "$CONTRACTS_JSON")

# Parse compose files from env var (space-separated string → array)
if [ -n "${COMPOSE_FILES_STR:-}" ]; then
    read -ra COMPOSE_FILES <<< "$COMPOSE_FILES_STR"
else
    COMPOSE_FILES=(-f "$DEPLOY_DIR/docker-compose.yml")
fi

cast_run() {
    docker compose "${COMPOSE_FILES[@]}" exec anvil cast "$@"
}

# --- Step 1: Mint USDAleph + send ETH for gas ---

echo "==> Funding test accounts with ETH and USDAleph..."
for i in "${!TEST_ADDRS[@]}"; do
    ADDR="${TEST_ADDRS[$i]}"
    echo "    $ADDR: 1 ETH + 200 USDAleph"

    cast_run send --rpc-url http://anvil:8545 --private-key "$DEPLOYER_KEY" \
        --value "1ether" "$ADDR" > /dev/null

    cast_run send --rpc-url http://anvil:8545 --private-key "$DEPLOYER_KEY" \
        "$USDALEPH_ADDR" "mint(address,uint256)" "$ADDR" "$MINT_AMOUNT" > /dev/null
done

# --- Step 2: Send USDAleph to credit contract for credit conversion ---

echo "==> Sending 100 USDAleph per account to credit contract..."
for i in "${!TEST_ADDRS[@]}"; do
    ADDR="${TEST_ADDRS[$i]}"
    KEY="${TEST_KEYS[$i]}"

    cast_run send --rpc-url http://anvil:8545 --private-key "$KEY" \
        "$USDALEPH_ADDR" "transfer(address,uint256)" "$CREDIT_ADDR" "$CREDIT_AMOUNT" > /dev/null
done

# --- Step 3: Wait for credit balances on CCN ---

echo "==> Waiting for credit balances on CCN (up to 120s)..."
for attempt in $(seq 1 24); do
    ALL_FUNDED=true
    for ADDR in "${TEST_ADDRS[@]}"; do
        BALANCE=$(curl -sf "$CCN_URL/api/v0/addresses/$ADDR/balance" 2>/dev/null \
            | jq -r '.credit_balance // 0' 2>/dev/null || echo 0)
        if [ "$BALANCE" = "0" ] || [ "$BALANCE" = "null" ]; then
            ALL_FUNDED=false
            break
        fi
    done
    if $ALL_FUNDED; then
        break
    fi
    if [ "$attempt" -eq 24 ]; then
        echo "WARNING: Not all credit balances appeared after 120s — check credit-service logs"
    fi
    sleep 5
done

# --- Step 4: Summary ---

echo ""
echo "============================================"
echo "  Testnet Account Summary"
echo "============================================"
echo ""
echo "  Privileged (do not use for testing):"
echo "    Deployer + Credit service:  $DEPLOYER_ADDR"
echo ""
echo "  Contracts:"
echo "    USDAleph token:             $USDALEPH_ADDR"
echo "    Credit contract:            $CREDIT_ADDR"
echo ""
echo "  Test accounts:"
for ADDR in "${TEST_ADDRS[@]}"; do
    CREDITS=$(curl -sf "$CCN_URL/api/v0/addresses/$ADDR/balance" 2>/dev/null \
        | jq -r '.credit_balance // 0' 2>/dev/null || echo "?")
    if [ "$CREDITS" != "?" ] && [ "$CREDITS" != "0" ]; then
        USD=$(awk "BEGIN {printf \"%.1f\", $CREDITS / 1000000}")
        printf "    %s  %s credits (%s USD)\n" "$ADDR" "$CREDITS" "$USD"
    else
        printf "    %s  %s credits\n" "$ADDR" "$CREDITS"
    fi
done
echo ""
echo "============================================"
