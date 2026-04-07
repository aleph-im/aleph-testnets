#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

# Required environment variables
: "${TESTNET_HOST:?TESTNET_HOST must be set}"
: "${TESTNET_USER:?TESTNET_USER must be set}"
: "${SSH_KEY_FILE:?SSH_KEY_FILE must be set (path to private key)}"

REMOTE="$TESTNET_USER@$TESTNET_HOST"
SSH_OPTS=(-o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -i "$SSH_KEY_FILE")
REMOTE_DIR="/opt/aleph-testnet"

echo "==> Generating .env from manifesto..."
python3 -c "
import yaml, sys

with open('$REPO_ROOT/manifesto.yml') as f:
    m = yaml.safe_load(f)

lines = []
for section in ('components', 'infrastructure'):
    for name, info in m.get(section, {}).items():
        prefix = name.upper().replace('-', '_')
        if 'image' in info:
            lines.append(f'{prefix}_IMAGE={info[\"image\"]}')
            lines.append(f'{prefix}_TAG={info[\"tag\"]}')

with open('$SCRIPT_DIR/.env', 'w') as f:
    f.write('\n'.join(lines) + '\n')
"

echo "==> Copying files to $REMOTE:$REMOTE_DIR ..."
ssh "${SSH_OPTS[@]}" "$REMOTE" "mkdir -p $REMOTE_DIR"
scp "${SSH_OPTS[@]}" \
    "$SCRIPT_DIR/docker-compose.yml" \
    "$SCRIPT_DIR/config.yml.tpl" \
    "$SCRIPT_DIR/001-update-ipfs-config.sh" \
    "$SCRIPT_DIR/001-create-scheduler-db.sql" \
    "$SCRIPT_DIR/.env" \
    "$REMOTE:$REMOTE_DIR/"

# config.yml.tpl is used as-is (no envsubst needed for phase 1)
ssh "${SSH_OPTS[@]}" "$REMOTE" "cp $REMOTE_DIR/config.yml.tpl $REMOTE_DIR/config.yml"

echo "==> Redeploying CCN stack..."
ssh "${SSH_OPTS[@]}" "$REMOTE" bash <<DEPLOY
set -euo pipefail
cd /opt/aleph-testnet

# Ensure P2P keys directory exists (first deploy)
mkdir -p /etc/pyaleph/keys

# Load image tags from .env
set -a
source .env
set +a

# Generate P2P keys if they don't exist yet
if [ ! -f /etc/pyaleph/keys/node-secret.pkcs8.der ]; then
    echo "Generating P2P keys..."
    docker run --rm \
        -v /etc/pyaleph/keys:/opt/pyaleph/keys \
        --entrypoint pyaleph \
        "\${PYALEPH_IMAGE}:\${PYALEPH_TAG}" \
        --gen-keys --key-dir /opt/pyaleph/keys
fi

# Stop and wipe
docker compose down -v || true
rm -rf /var/lib/pyaleph/storage/*

# Pull and start
docker compose pull
docker compose up -d
DEPLOY

echo "==> Waiting for CCN to become ready..."
for i in $(seq 1 24); do
    if ssh "${SSH_OPTS[@]}" "$REMOTE" "curl -sf http://localhost:4024/api/v0/version > /dev/null 2>&1"; then
        echo "==> CCN is ready!"
        exit 0
    fi
    echo "    Waiting... ($((i * 5))s / 120s)"
    sleep 5
done

echo "ERROR: CCN did not become ready within 120s"
exit 1
