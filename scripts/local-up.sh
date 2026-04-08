#!/usr/bin/env bash
# Spin up the CCN stack locally and run integration tests.
# All data is stored under .local/ in the repo — no sudo needed.
#
# Usage:
#   ./scripts/local-up.sh                   # full run: env → up → deploy → test
#   ./scripts/local-up.sh --env             # generate .env, install deps, download CLI
#   ./scripts/local-up.sh --up              # start containers, wait for CCN
#   ./scripts/local-up.sh --deploy-contracts # deploy contracts, start indexer, fund accounts
#   ./scripts/local-up.sh --test            # run pytest (extra args passed through)
#   ./scripts/local-up.sh --crn-up          # provision + install CRN on DigitalOcean
#   ./scripts/local-up.sh --crn-down        # destroy CRN droplets
#   ./scripts/local-up.sh --logs            # dump all container logs
#   ./scripts/local-up.sh --down            # tear down stack and wipe state
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
DEPLOY_DIR="$REPO_ROOT/deploy"
LOCAL_DIR="$REPO_ROOT/.local"
BIN_DIR="$REPO_ROOT/bin"
CCN_URL="http://localhost:4024"

COMPOSE_FILES=(-f "$DEPLOY_DIR/docker-compose.yml" -f "$DEPLOY_DIR/docker-compose.local.yml")

# Test private key: Anvil account #4 (accounts #0-#3 are reserved for privileged ops)
TEST_PRIVATE_KEY="47e179ec197488593b187f80a00eb0da91f1b9d0b13f8733639f19c30a34926a"

setup_env() {
    echo "==> Installing Python dependencies..."
    export PIP_BREAK_SYSTEM_PACKAGES=1
    pip install "$REPO_ROOT"

    echo "==> Generating .env from manifesto..."
    python3 -c "
import yaml
with open('$REPO_ROOT/manifesto.yml') as f:
    m = yaml.safe_load(f)
for section in ('components', 'infrastructure'):
    for name, info in m.get(section, {}).items():
        prefix = name.upper().replace('-', '_')
        if 'image' in info:
            print(f'{prefix}_IMAGE={info[\"image\"]}')
            print(f'{prefix}_TAG={info[\"tag\"]}')
" > "$DEPLOY_DIR/.env"

    if [ -x "$BIN_DIR/aleph" ]; then
        echo "==> CLI binary already exists at $BIN_DIR/aleph"
        return
    fi
    echo "==> Downloading Aleph CLI..."
    mkdir -p "$BIN_DIR"
    CLI_URL=$(python3 -c "
import yaml
with open('$REPO_ROOT/manifesto.yml') as f:
    m = yaml.safe_load(f)
print(m['components']['aleph-cli']['url'])
")
    curl -fsSL "$CLI_URL" -o "$BIN_DIR/aleph"
    chmod +x "$BIN_DIR/aleph"
    echo "==> CLI downloaded to $BIN_DIR/aleph"
}

wait_for_ccn() {
    echo "==> Waiting for CCN to become ready..."
    for i in $(seq 1 36); do
        if curl -sf "$CCN_URL/api/v0/version" > /dev/null 2>&1; then
            echo "==> CCN is ready!"
            return 0
        fi
        echo "    Waiting... ($((i * 5))s / 180s)"
        sleep 5
    done
    echo "ERROR: CCN did not become ready within 180s"
    echo "       Check logs with: docker compose ${COMPOSE_FILES[*]} logs"
    return 1
}

download_rootfs() {
    local rootfs="$LOCAL_DIR/rootfs.img"
    if [ -f "$rootfs" ]; then
        echo "==> Rootfs already downloaded"
        return
    fi
    echo "==> Downloading Ubuntu 24.04 cloud image..."
    mkdir -p "$LOCAL_DIR"
    curl -fSL -o "$rootfs" \
        "https://cloud-images.ubuntu.com/noble/current/noble-server-cloudimg-amd64.img"
    echo "==> Rootfs downloaded to $rootfs"
}

stack_up() {
    # Source .env so image tags are available for key generation
    set -a
    source "$DEPLOY_DIR/.env"
    set +a

    # Create local data directories
    mkdir -p "$LOCAL_DIR/keys"

    # Copy config
    cp "$DEPLOY_DIR/config.yml.tpl" "$DEPLOY_DIR/config.yml"

    # Generate P2P keys if they don't exist yet
    if [ ! -f "$LOCAL_DIR/keys/node-secret.pkcs8.der" ]; then
        echo "==> Generating P2P keys..."
        docker run --rm \
            --user root \
            -v "$LOCAL_DIR/keys:/opt/pyaleph/keys" \
            --entrypoint pyaleph \
            "${PYALEPH_IMAGE}:${PYALEPH_TAG}" \
            --gen-keys --key-dir /opt/pyaleph/keys
    fi

    echo "==> Starting CCN stack..."
    docker compose "${COMPOSE_FILES[@]}" up -d

    wait_for_ccn
}

deploy_contracts() {
    download_rootfs

    # Export compose files as a space-separated string for child scripts
    export COMPOSE_FILES_STR="${COMPOSE_FILES[*]}"

    # Deploy contracts on Anvil
    echo "==> Deploying contracts on Anvil..."
    "$REPO_ROOT/scripts/deploy-contracts.sh"

    # Start the indexer and credit service (uses 'credits' profile)
    echo "==> Starting indexer and credit service..."
    docker compose "${COMPOSE_FILES[@]}" --profile credits up -d indexer credit-service

    # Start nodestatus services (corechannel aggregates + balance tracking)
    echo "==> Starting nodestatus services..."
    docker compose "${COMPOSE_FILES[@]}" --profile nodestatus up -d nodestatus nodestatus-balances

    # Start scheduler services (scheduler-rs + scheduler-api)
    echo "==> Starting scheduler services..."
    docker compose "${COMPOSE_FILES[@]}" --profile scheduler up -d scheduler-rs scheduler-api

    # Wait for indexer to be ready
    echo "==> Waiting for indexer..."
    for i in $(seq 1 24); do
        if curl -sf "http://localhost:8081/graphql" -X POST -H "Content-Type: application/json" -d '{"query":"{__typename}"}' > /dev/null 2>&1; then
            echo "==> Indexer is ready!"
            break
        fi
        if [ "$i" -eq 24 ]; then
            echo "WARNING: Indexer may not be ready (120s timeout)"
        fi
        sleep 5
    done

    # Fund test accounts
    echo "==> Funding test accounts..."
    "$REPO_ROOT/scripts/fund-test-accounts.sh"
}

crn_up() {
    # Determine the CCN host (the machine running docker compose).
    # In CI this is the droplet's public IP; locally it's the host's external IP.
    local ccn_host="${CCN_HOST:-}"
    if [ -z "$ccn_host" ]; then
        # Try to detect the external IP
        ccn_host=$(curl -sf https://ifconfig.me 2>/dev/null || hostname -I | awk '{print $1}')
    fi
    export CCN_HOST="$ccn_host"
    echo "==> Provisioning CRN(s) with CCN_HOST=$CCN_HOST"
    "$REPO_ROOT/scripts/crn-up.sh" --provision
    "$REPO_ROOT/scripts/crn-up.sh" --install
    "$REPO_ROOT/scripts/crn-up.sh" --register
}

crn_down() {
    "$REPO_ROOT/scripts/crn-up.sh" --destroy
}

run_tests() {
    echo "==> Running integration tests..."
    export PATH="$BIN_DIR:$PATH"
    export ALEPH_TESTNET_CCN_URL="$CCN_URL"
    export ALEPH_TESTNET_PRIVATE_KEY="$TEST_PRIVATE_KEY"
    export ALEPH_TESTNET_INDEXER_URL="http://localhost:8081"
    export ALEPH_TESTNET_CONTRACTS_JSON="$LOCAL_DIR/contracts.json"
    export ALEPH_TESTNET_ANVIL_RPC="http://localhost:8545"
    export ALEPH_TESTNET_SCHEDULER_API_URL="http://localhost:8082"
    export ALEPH_TESTNET_ROOTFS="$LOCAL_DIR/rootfs.img"
    cd "$REPO_ROOT"
    pytest -v --junitxml=results.xml "$@"
}

dump_logs() {
    echo "==> Dumping container logs..."
    for svc in $(docker compose "${COMPOSE_FILES[@]}" --profile credits --profile nodestatus --profile scheduler config --services 2>/dev/null); do
        echo "===== $svc ====="
        docker compose "${COMPOSE_FILES[@]}" --profile credits --profile nodestatus --profile scheduler logs --no-color --tail=200 "$svc" 2>/dev/null || true
    done
}

stack_down() {
    echo "==> Stopping CCN stack..."
    docker compose "${COMPOSE_FILES[@]}" --profile credits --profile nodestatus --profile scheduler down -v || true

    # Destroy CRN droplets if any were provisioned
    if [ -d "$LOCAL_DIR/crn" ]; then
        "$REPO_ROOT/scripts/crn-up.sh" --destroy || true
    fi

    rm -rf "$LOCAL_DIR"
    rm -f "$DEPLOY_DIR/.env" "$DEPLOY_DIR/config.yml"
    rm -rf "$DEPLOY_DIR/indexer"
    rm -rf "$REPO_ROOT/contracts/broadcast"
    echo "==> Stack stopped and state wiped."
}

case "${1:-}" in
    --env)
        setup_env
        ;;
    --up)
        stack_up
        ;;
    --deploy-contracts)
        deploy_contracts
        ;;
    --crn-up)
        crn_up
        ;;
    --crn-down)
        crn_down
        ;;
    --download-rootfs)
        download_rootfs
        ;;
    --test)
        shift
        run_tests "$@"
        ;;
    --logs)
        dump_logs
        ;;
    --down)
        stack_down
        ;;
    "")
        setup_env
        stack_up
        deploy_contracts
        run_tests
        ;;
    --help|-h)
        echo "Usage: $0 [--env|--up|--deploy-contracts|--download-rootfs|--crn-up|--crn-down|--test|--logs|--down]"
        exit 0
        ;;
    *)
        echo "Usage: $0 [--env|--up|--deploy-contracts|--download-rootfs|--crn-up|--crn-down|--test|--logs|--down]"
        exit 1
        ;;
esac
