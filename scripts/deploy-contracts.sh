#!/usr/bin/env bash
# Deploy smart contracts on Anvil and generate indexer configuration.
# Accepts compose file args via COMPOSE_FILES_STR env var (space-separated)
# or defaults to deploy/docker-compose.yml.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
DEPLOY_DIR="$REPO_ROOT/deploy"
LOCAL_DIR="$REPO_ROOT/.local"
CONTRACTS_DIR="$REPO_ROOT/contracts"
CONTRACTS_JSON="$LOCAL_DIR/contracts.json"

# Anvil account 0 — deterministic deployer
DEPLOYER_KEY="0xac0974bec39a17e36ba4a6b4d238ff944bacb478cbed5efcae784d7bf4f2ff80"
DEPLOYER_ADDR="0xf39Fd6e51aad88F6F4ce6aB8827279cffFb92266"

# Parse compose files from env var (space-separated string → array)
if [ -n "${COMPOSE_FILES_STR:-}" ]; then
    read -ra COMPOSE_FILES <<< "$COMPOSE_FILES_STR"
else
    COMPOSE_FILES=(-f "$DEPLOY_DIR/docker-compose.yml")
fi

# Helper: run forge/cast inside the Foundry container on the pyaleph network
foundry_run() {
    docker compose "${COMPOSE_FILES[@]}" run --rm \
        --user root \
        -v "$CONTRACTS_DIR:/contracts" \
        -w /contracts \
        -e DEPLOYER_PRIVATE_KEY="$DEPLOYER_KEY" \
        --entrypoint "" \
        anvil "$@"
}

echo "==> Waiting for Anvil..."
for i in $(seq 1 12); do
    if foundry_run cast block-number --rpc-url http://anvil:8545 > /dev/null 2>&1; then
        echo "==> Anvil is ready"
        break
    fi
    if [ "$i" -eq 12 ]; then
        echo "ERROR: Anvil not ready after 60s"
        exit 1
    fi
    sleep 5
done

echo "==> Deploying contracts..."
foundry_run forge script script/Deploy.s.sol \
    --rpc-url http://anvil:8545 \
    --broadcast \
    --code-size-limit 100000 \
    -v

# Parse addresses from Foundry broadcast artifacts
BROADCAST="$CONTRACTS_DIR/broadcast/Deploy.s.sol/31337/run-latest.json"

if [ ! -f "$BROADCAST" ]; then
    echo "ERROR: Broadcast file not found at $BROADCAST"
    exit 1
fi

# Parse addresses and convert to checksummed format (ethers.js uses checksummed)
USDALEPH_ADDR=$(jq -r '[.transactions[] | select(.contractName == "USDAleph" and .transactionType == "CREATE")] | first | .contractAddress' "$BROADCAST")
USDALEPH_ADDR=$(foundry_run cast to-check-sum-address "$USDALEPH_ADDR")
MOCKALEPH_ADDR=$(jq -r '[.transactions[] | select(.contractName == "MockALEPH" and .transactionType == "CREATE")] | first | .contractAddress' "$BROADCAST")
MOCKALEPH_ADDR=$(foundry_run cast to-check-sum-address "$MOCKALEPH_ADDR")
# The proxy is ERC1967Proxy — that's the credit contract address
CREDIT_ADDR=$(jq -r '[.transactions[] | select(.contractName == "ERC1967Proxy" and .transactionType == "CREATE")] | first | .contractAddress' "$BROADCAST")
CREDIT_ADDR=$(foundry_run cast to-check-sum-address "$CREDIT_ADDR")

if [ -z "$USDALEPH_ADDR" ] || [ -z "$MOCKALEPH_ADDR" ] || [ -z "$CREDIT_ADDR" ]; then
    echo "ERROR: Failed to parse contract addresses from broadcast"
    echo "Broadcast contents:"
    jq '.transactions[] | {contractName, contractAddress}' "$BROADCAST"
    exit 1
fi

echo "==> Contract addresses:"
echo "    USDAleph:               $USDALEPH_ADDR"
echo "    MockALEPH:              $MOCKALEPH_ADDR"
echo "    AlephPaymentProcessor:  $CREDIT_ADDR"

# Write addresses JSON
mkdir -p "$LOCAL_DIR"
cat > "$CONTRACTS_JSON" <<EOF
{
    "usdaleph": "$USDALEPH_ADDR",
    "mock_aleph": "$MOCKALEPH_ADDR",
    "credit_contract": "$CREDIT_ADDR"
}
EOF
echo "==> Wrote $CONTRACTS_JSON"

# Generate indexer chains.yaml
INDEXER_DIR="$DEPLOY_DIR/indexer"
mkdir -p "$INDEXER_DIR"
cat > "$INDEXER_DIR/chains.yaml" <<EOF
ethereum:
  tokenContracts:
    USDC:
      address: "$USDALEPH_ADDR"
      decimals: 6
    ALEPH:
      address: "$MOCKALEPH_ADDR"
      decimals: 18
  creditContract:
    address: "$CREDIT_ADDR"
    nativePayments: false
  providers: {}
EOF
echo "==> Wrote $INDEXER_DIR/chains.yaml"

# Copy ABIs for the indexer
ABI_DIR="$INDEXER_DIR/abis/ethereum/abi"
mkdir -p "$ABI_DIR"

# Use lowercase addresses for ABI filenames (indexer convention)
USDALEPH_LOWER=$(echo "$USDALEPH_ADDR" | tr '[:upper:]' '[:lower:]')
MOCKALEPH_LOWER=$(echo "$MOCKALEPH_ADDR" | tr '[:upper:]' '[:lower:]')
CREDIT_LOWER=$(echo "$CREDIT_ADDR" | tr '[:upper:]' '[:lower:]')

# Extract just the ABI array from Foundry artifacts
jq '.abi' "$CONTRACTS_DIR/out/USDAleph.sol/USDAleph.json" > "$ABI_DIR/${USDALEPH_LOWER}.json"
jq '.abi' "$CONTRACTS_DIR/out/MockALEPH.sol/MockALEPH.json" > "$ABI_DIR/${MOCKALEPH_LOWER}.json"
jq '.abi' "$CONTRACTS_DIR/out/AlephPaymentProcessor.sol/AlephPaymentProcessor.json" > "$ABI_DIR/${CREDIT_LOWER}.json"
echo "==> Copied ABIs to $ABI_DIR/"

# Append addresses to .env for docker-compose variable substitution
if ! grep -q "CREDIT_SENDER_ADDRESS" "$DEPLOY_DIR/.env" 2>/dev/null; then
    echo "CREDIT_SENDER_ADDRESS=$DEPLOYER_ADDR" >> "$DEPLOY_DIR/.env"
    echo "CREDIT_SENDER_PRIVATE_KEY=$DEPLOYER_KEY" >> "$DEPLOY_DIR/.env"
fi
if ! grep -q "USDALEPH_ADDRESS" "$DEPLOY_DIR/.env" 2>/dev/null; then
    echo "USDALEPH_ADDRESS=$USDALEPH_ADDR" >> "$DEPLOY_DIR/.env"
    echo "MOCKALEPH_ADDRESS=$MOCKALEPH_ADDR" >> "$DEPLOY_DIR/.env"
    echo "CREDIT_CONTRACT_ADDRESS=$CREDIT_ADDR" >> "$DEPLOY_DIR/.env"
    # Set distribution start date to "now" so the credit service's start date
    # falls within the indexer's processed range (Anvil has no old blocks).
    DISTRIBUTION_START_MS=$(date +%s)000
    echo "DISTRIBUTION_START_DATE=$DISTRIBUTION_START_MS" >> "$DEPLOY_DIR/.env"
fi

echo "==> Contract deployment complete!"
