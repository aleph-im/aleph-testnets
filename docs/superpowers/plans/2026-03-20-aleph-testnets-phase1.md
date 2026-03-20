# Aleph Testnets Phase 1 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Deploy a pyaleph CCN to a permanent testnet server and run nightly integration tests against it using the Aleph Rust CLI.

**Architecture:** A manifesto YAML pins component versions. On merge to main, a deploy workflow SSHes into the testnet server, wipes state, and brings up the CCN stack via Docker Compose. A nightly workflow downloads the pinned CLI binary and runs pytest-based integration tests against the live CCN.

**Tech Stack:** Docker Compose, bash (deploy scripts, envsubst), Python/pytest (integration tests), GitHub Actions (CI/CD), Aleph Rust CLI (test driver)

**Spec:** `docs/superpowers/specs/2026-03-20-aleph-testnets-design.md`

---

## File Map

| File | Responsibility |
|---|---|
| `manifesto.yml` | Version pins for all components and infrastructure images |
| `deploy/docker-compose.yml` | CCN stack definition with env var substitution for image tags |
| `deploy/config.yml.tpl` | pyaleph config template, rendered by envsubst |
| `deploy/001-update-ipfs-config.sh` | IPFS init script (copied from pyaleph repo, simplified for testnet) |
| `deploy/deploy.sh` | Parses manifesto, generates .env, SCPs files, SSHes in, redeploys |
| `pyproject.toml` | Python project config: pytest + pyyaml deps |
| `tests/conftest.py` | Shared fixtures: CLI wrapper, CCN readiness check, temp files |
| `tests/test_files.py` | File upload/download integration tests |
| `tests/test_posts.py` | Post create/list/amend integration tests |
| `tests/test_aggregates.py` | Aggregate create/read integration tests |
| `tests/test_messages.py` | Message list/forget integration tests |
| `.github/workflows/deploy.yml` | Deploy-on-merge workflow |
| `.github/workflows/nightly.yml` | Nightly test run workflow |

---

### Task 1: Manifesto and pyproject.toml

**Files:**
- Create: `manifesto.yml`
- Create: `pyproject.toml`

- [ ] **Step 1: Create manifesto.yml**

```yaml
# manifesto.yml — Version pins for testnet components
#
# "components" are the software under test. PRs bump these.
# "infrastructure" contains companion services that change infrequently.

components:
  pyaleph:
    image: alephim/pyaleph-node
    tag: "0.10.1-rc0"

  p2p-service:
    image: alephim/p2p-service
    tag: "0.1.4"

  aleph-cli:
    version: "0.7.1"
    url: "https://github.com/aleph-im/aleph-rs/releases/download/v0.7.1/aleph-cli-linux-x86_64"

infrastructure:
  postgres:
    image: postgres
    tag: "15.1"
  rabbitmq:
    image: rabbitmq
    tag: "3.11.15-management"
  redis:
    image: redis
    tag: "8.4.0"
  ipfs:
    image: ipfs/kubo
    tag: "v0.37.0"
  anvil:
    image: ghcr.io/foundry-rs/foundry
    tag: "stable"
```

- [ ] **Step 2: Create pyproject.toml**

```toml
[project]
name = "aleph-testnets"
version = "0.1.0"
description = "Integration tests for Aleph Cloud testnets"
requires-python = ">=3.10"
dependencies = [
    "pytest>=8.0",
    "pyyaml>=6.0",
]

[tool.pytest.ini_options]
testpaths = ["tests"]
```

- [ ] **Step 3: Commit**

```bash
git add manifesto.yml pyproject.toml
git commit -m "feat: add manifesto and pyproject.toml"
```

---

### Task 2: Docker Compose for CCN Stack

**Files:**
- Create: `deploy/docker-compose.yml`
- Create: `deploy/config.yml.tpl`
- Create: `deploy/001-update-ipfs-config.sh`

**Reference:** pyaleph's `deployment/samples/docker-compose/docker-compose.yml` and `deployment/scripts/001-update-ipfs-config.sh`

- [ ] **Step 1: Create deploy/docker-compose.yml**

The compose file uses env vars (from `.env`) for all image tags. The P2P keys directory is bind-mounted from `/etc/pyaleph/keys/` on the host. pyaleph storage is bind-mounted from `/var/lib/pyaleph/` so it can be wiped independently.

```yaml
volumes:
  pyaleph-postgres:
  pyaleph-rabbitmq:
  pyaleph-redis:
  pyaleph-ipfs:

services:
  pyaleph:
    restart: always
    image: ${PYALEPH_IMAGE}:${PYALEPH_TAG}
    command: --config /opt/pyaleph/config.yml --key-dir /opt/pyaleph/keys -v
    volumes:
      - ./config.yml:/opt/pyaleph/config.yml:ro
      - /etc/pyaleph/keys:/opt/pyaleph/keys
      - /var/lib/pyaleph:/var/lib/pyaleph
    depends_on:
      - postgres
      - ipfs
      - p2p-service
      - redis
      - anvil
    networks:
      - pyaleph
    logging:
      options:
        max-size: 50m

  pyaleph-api:
    restart: always
    image: ${PYALEPH_IMAGE}:${PYALEPH_TAG}
    command: --config /opt/pyaleph/config.yml --key-dir /opt/pyaleph/keys -v
    entrypoint: ["bash", "deployment/scripts/run_aleph_ccn_api.sh"]
    ports:
      - "4024:4024/tcp"
    volumes:
      - ./config.yml:/opt/pyaleph/config.yml:ro
      - /var/lib/pyaleph:/var/lib/pyaleph
    environment:
      CCN_CONFIG_API_PORT: 4024
      CCN_CONFIG_API_NB_WORKERS: 4
    depends_on:
      - pyaleph
    networks:
      - pyaleph
    logging:
      options:
        max-size: 50m

  p2p-service:
    restart: always
    image: ${P2P_SERVICE_IMAGE}:${P2P_SERVICE_TAG}
    networks:
      - pyaleph
    volumes:
      - ./config.yml:/etc/p2p-service/config.yml:ro
      - /etc/pyaleph/keys/node-secret.pkcs8.der:/etc/p2p-service/node-secret.pkcs8.der:ro
    depends_on:
      - rabbitmq
    environment:
      RUST_LOG: info
    ports:
      - "4025:4025"
      - "127.0.0.1:4030:4030"
    command:
      - "--config"
      - "/etc/p2p-service/config.yml"
      - "--private-key-file"
      - "/etc/p2p-service/node-secret.pkcs8.der"

  postgres:
    restart: always
    image: ${POSTGRES_IMAGE}:${POSTGRES_TAG}
    ports:
      - "127.0.0.1:5432:5432"
    volumes:
      - pyaleph-postgres:/var/lib/postgresql/data
    environment:
      POSTGRES_USER: aleph
      POSTGRES_PASSWORD: decentralize-everything
      POSTGRES_DB: aleph
    networks:
      - pyaleph
    shm_size: "2gb"

  rabbitmq:
    restart: always
    image: ${RABBITMQ_IMAGE}:${RABBITMQ_TAG}
    networks:
      - pyaleph
    environment:
      RABBITMQ_DEFAULT_USER: aleph-p2p
      RABBITMQ_DEFAULT_PASS: change-me!
    ports:
      - "127.0.0.1:5672:5672"
      - "127.0.0.1:15672:15672"
    volumes:
      - pyaleph-rabbitmq:/var/lib/rabbitmq

  redis:
    restart: always
    image: ${REDIS_IMAGE}:${REDIS_TAG}
    networks:
      - pyaleph
    volumes:
      - pyaleph-redis:/data

  ipfs:
    restart: always
    image: ${IPFS_IMAGE}:${IPFS_TAG}
    ports:
      - "4001:4001"
      - "4001:4001/udp"
      - "127.0.0.1:5001:5001"
    volumes:
      - pyaleph-ipfs:/data/ipfs
      - ./001-update-ipfs-config.sh:/container-init.d/001-update-ipfs-config.sh:ro
    environment:
      - IPFS_PROFILE=server
      - IPFS_TELEMETRY=off
    networks:
      - pyaleph
    command: ["daemon", "--enable-pubsub-experiment", "--enable-gc", "--migrate"]

  anvil:
    restart: always
    image: ${ANVIL_IMAGE}:${ANVIL_TAG}
    networks:
      - pyaleph
    entrypoint: ["anvil", "--host", "0.0.0.0", "--port", "8545"]

networks:
  pyaleph:
```

- [ ] **Step 2: Create deploy/config.yml.tpl**

Testnet-specific pyaleph config. Envsubst variables are not used here since the config values are static for the testnet — this file is a ready-to-use config, not a template that needs rendering. Keep the `.tpl` extension as a convention to signal it's managed by this repo, but it can be used as-is.

```yaml
---
ethereum:
  enabled: true
  api_url: http://anvil:8545
  chain_id: 31337
  packing_node: false
  sync_contract: "0x0000000000000000000000000000000000000000"
  start_height: 0

nuls2:
  enabled: false

bsc:
  enabled: false

tezos:
  enabled: false

postgres:
  host: postgres
  port: 5432
  database: aleph
  user: aleph
  password: decentralize-everything

storage:
  store_files: true
  engine: filesystem
  folder: /var/lib/pyaleph

ipfs:
  enabled: true
  host: ipfs
  port: 5001
  gateway_port: 8080

aleph:
  queue_topic: ALEPH-TESTNET

p2p:
  daemon_host: p2p-service
  http_port: 4024
  port: 4025
  control_port: 4030
  reconnect_delay: 60
  peers: []

rabbitmq:
  host: rabbitmq
  port: 5672
  username: aleph-p2p
  password: change-me!

redis:
  host: redis
  port: 6379

sentry:
  dsn: ""
```

- [ ] **Step 3: Create deploy/001-update-ipfs-config.sh**

Simplified version of pyaleph's IPFS init script — no production bootstrap peers, just basic settings for testnet.

```bash
#!/bin/sh
# Minimal IPFS config for testnet use

echo "Updating IPFS config for testnet..."

ipfs config Reprovider.Strategy 'pinned'
ipfs config Routing.Type "dhtserver"
ipfs config Datastore.StorageMax '5GB'
ipfs config Datastore.GCPeriod '12h'
ipfs config Bootstrap --json '[]'

echo "IPFS config updated."
```

- [ ] **Step 4: Verify compose file is valid**

Run: `docker compose -f deploy/docker-compose.yml config` (with a dummy .env)

```bash
cd deploy
cat > .env <<'EOF'
PYALEPH_IMAGE=alephim/pyaleph-node
PYALEPH_TAG=0.10.1-rc0
P2P_SERVICE_IMAGE=alephim/p2p-service
P2P_SERVICE_TAG=0.1.4
POSTGRES_IMAGE=postgres
POSTGRES_TAG=15.1
RABBITMQ_IMAGE=rabbitmq
RABBITMQ_TAG=3.11.15-management
REDIS_IMAGE=redis
REDIS_TAG=8.4.0
IPFS_IMAGE=ipfs/kubo
IPFS_TAG=v0.37.0
ANVIL_IMAGE=ghcr.io/foundry-rs/foundry
ANVIL_TAG=stable
EOF
docker compose config > /dev/null
rm .env
cd ..
```

Expected: no errors.

- [ ] **Step 5: Commit**

```bash
git add deploy/
git commit -m "feat: add Docker Compose stack and pyaleph config for testnet CCN"
```

---

### Task 3: Deploy Script

**Files:**
- Create: `deploy/deploy.sh`

- [ ] **Step 1: Create deploy/deploy.sh**

The script parses `manifesto.yml` using Python (since `pyyaml` is a project dependency), generates a `.env`, SCPs files to the server, and runs the redeploy sequence over SSH.

```bash
#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

# Required environment variables
: "${TESTNET_HOST:?TESTNET_HOST must be set}"
: "${TESTNET_USER:?TESTNET_USER must be set}"
: "${SSH_KEY_FILE:?SSH_KEY_FILE must be set (path to private key)}"

REMOTE="$TESTNET_USER@$TESTNET_HOST"
SSH_OPTS="-o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -i $SSH_KEY_FILE"
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
ssh $SSH_OPTS "$REMOTE" "mkdir -p $REMOTE_DIR"
scp $SSH_OPTS \
    "$SCRIPT_DIR/docker-compose.yml" \
    "$SCRIPT_DIR/config.yml.tpl" \
    "$SCRIPT_DIR/001-update-ipfs-config.sh" \
    "$SCRIPT_DIR/.env" \
    "$REMOTE:$REMOTE_DIR/"

# config.yml.tpl is used as-is (no envsubst needed for phase 1)
ssh $SSH_OPTS "$REMOTE" "cp $REMOTE_DIR/config.yml.tpl $REMOTE_DIR/config.yml"

echo "==> Redeploying CCN stack..."
ssh $SSH_OPTS "$REMOTE" bash <<DEPLOY
set -euo pipefail
cd $REMOTE_DIR

# Ensure P2P keys directory exists (first deploy)
mkdir -p /etc/pyaleph/keys

# Stop and wipe
docker compose down -v || true
rm -rf /var/lib/pyaleph/storage/*

# Pull and start
docker compose pull
docker compose up -d
DEPLOY

echo "==> Waiting for CCN to become ready..."
for i in $(seq 1 24); do
    if ssh $SSH_OPTS "$REMOTE" "curl -sf http://localhost:4024/api/v0/version > /dev/null 2>&1"; then
        echo "==> CCN is ready!"
        exit 0
    fi
    echo "    Waiting... ($((i * 5))s / 120s)"
    sleep 5
done

echo "ERROR: CCN did not become ready within 120s"
exit 1
```

- [ ] **Step 2: Make it executable**

```bash
chmod +x deploy/deploy.sh
```

- [ ] **Step 3: Test the script locally (dry-run of the parsing logic)**

```bash
cd /home/olivier/git/aleph/aleph-testnets
python3 -c "
import yaml
with open('manifesto.yml') as f:
    m = yaml.safe_load(f)
for section in ('components', 'infrastructure'):
    for name, info in m.get(section, {}).items():
        prefix = name.upper().replace('-', '_')
        if 'image' in info:
            print(f'{prefix}_IMAGE={info[\"image\"]}')
            print(f'{prefix}_TAG={info[\"tag\"]}')
"
```

Expected output:
```
PYALEPH_IMAGE=alephim/pyaleph-node
PYALEPH_TAG=0.10.1-rc0
P2P_SERVICE_IMAGE=alephim/p2p-service
P2P_SERVICE_TAG=0.1.4
POSTGRES_IMAGE=postgres
POSTGRES_TAG=15.1
RABBITMQ_IMAGE=rabbitmq
RABBITMQ_TAG=3.11.15-management
REDIS_IMAGE=redis
REDIS_TAG=8.4.0
IPFS_IMAGE=ipfs/kubo
IPFS_TAG=v0.37.0
ANVIL_IMAGE=ghcr.io/foundry-rs/foundry
ANVIL_TAG=stable
```

- [ ] **Step 4: Commit**

```bash
git add deploy/deploy.sh
git commit -m "feat: add deploy script for testnet CCN"
```

---

### Task 4: Test Fixtures (conftest.py)

**Files:**
- Create: `tests/__init__.py`
- Create: `tests/conftest.py`

- [ ] **Step 1: Create tests/__init__.py**

Empty file.

- [ ] **Step 2: Create tests/conftest.py**

```python
import json
import os
import subprocess
import time
import uuid

import pytest
import urllib.request
import urllib.error


def _require_env(name: str) -> str:
    val = os.environ.get(name)
    if not val:
        pytest.fail(f"Required environment variable {name} is not set")
    return val


@pytest.fixture(scope="session")
def ccn_url() -> str:
    return _require_env("ALEPH_TESTNET_CCN_URL").rstrip("/")


@pytest.fixture(scope="session")
def private_key() -> str:
    return _require_env("ALEPH_TESTNET_PRIVATE_KEY")


@pytest.fixture(scope="session", autouse=True)
def ccn_ready(ccn_url: str):
    """Wait for the CCN to be reachable before running any tests."""
    url = f"{ccn_url}/api/v0/version"
    deadline = time.time() + 120
    last_err = None
    while time.time() < deadline:
        try:
            req = urllib.request.urlopen(url, timeout=5)
            if req.status == 200:
                return
        except (urllib.error.URLError, OSError) as e:
            last_err = e
        time.sleep(5)
    pytest.fail(f"CCN not ready at {url} after 120s: {last_err}")


@pytest.fixture(scope="session")
def aleph_cli(ccn_url: str, private_key: str):
    """Return a function that invokes the aleph CLI with pre-configured flags.

    Usage:
        result = aleph_cli("file", "upload", "/path/to/file")
        result = aleph_cli("post", "list", "--channels", "test", parse_json=True)
    """
    def run(*args: str, parse_json: bool = False, check: bool = True) -> subprocess.CompletedProcess | dict:
        cmd = [
            "aleph",
            "--ccn-url", ccn_url,
            "--json",
            *args,
        ]
        # For commands that need signing, inject the private key via env var
        env = {**os.environ, "ALEPH_PRIVATE_KEY": private_key}
        result = subprocess.run(cmd, capture_output=True, text=True, env=env)
        if check and result.returncode != 0:
            pytest.fail(
                f"CLI command failed: {' '.join(cmd)}\n"
                f"Exit code: {result.returncode}\n"
                f"Stdout: {result.stdout}\n"
                f"Stderr: {result.stderr}"
            )
        if parse_json:
            return json.loads(result.stdout)
        return result
    return run


@pytest.fixture
def unique_channel() -> str:
    """Generate a unique channel name to avoid test collisions."""
    return f"test-{uuid.uuid4().hex[:12]}"


@pytest.fixture
def tmp_file(tmp_path):
    """Create a temporary file with random content for upload tests."""
    f = tmp_path / "testfile.bin"
    f.write_bytes(os.urandom(1024))
    return f
```

- [ ] **Step 3: Verify fixtures load without errors**

```bash
cd /home/olivier/git/aleph/aleph-testnets
pip install -e . 2>/dev/null || pip install pytest pyyaml
pytest tests/conftest.py --collect-only 2>&1 | head -5
```

Expected: no import errors (tests won't run without env vars, but fixtures should load).

- [ ] **Step 4: Commit**

```bash
git add tests/ pyproject.toml
git commit -m "feat: add test fixtures — CLI wrapper, CCN readiness, helpers"
```

---

### Task 5: File Integration Tests

**Files:**
- Create: `tests/test_files.py`

- [ ] **Step 1: Write test_file_upload_and_download**

```python
def test_file_upload_and_download(aleph_cli, tmp_file, tmp_path):
    """Upload a file and download it by hash, compare bytes."""
    result = aleph_cli("file", "upload", str(tmp_file), parse_json=True)
    file_hash = result["item_hash"]
    assert file_hash, "Upload should return an item_hash"

    out = tmp_path / "downloaded.bin"
    aleph_cli("file", "download", "--hash", file_hash, "--output", str(out))
    assert out.read_bytes() == tmp_file.read_bytes(), "Downloaded file should match uploaded file"


def test_file_upload_with_ref(aleph_cli, tmp_file, tmp_path):
    """Upload a file with a user-defined ref, download by ref."""
    ref = f"test-ref-{__import__('uuid').uuid4().hex[:8]}"
    result = aleph_cli("file", "upload", str(tmp_file), "--ref", ref, parse_json=True)
    assert result["item_hash"], "Upload should return an item_hash"

    out = tmp_path / "downloaded_ref.bin"
    aleph_cli("file", "download", "--ref", ref, "--output", str(out))
    assert out.read_bytes() == tmp_file.read_bytes(), "Downloaded file should match uploaded file"
```

- [ ] **Step 2: Commit**

```bash
git add tests/test_files.py
git commit -m "feat: add file upload/download integration tests"
```

---

### Task 6: Post Integration Tests

**Files:**
- Create: `tests/test_posts.py`

- [ ] **Step 1: Write post tests**

```python
import time


def test_post_create_and_list(aleph_cli, unique_channel):
    """Create a post, list posts by channel, verify it appears."""
    content = '{"body": "hello from integration test"}'
    result = aleph_cli(
        "post", "create",
        "--type", "test-post",
        "--content", content,
        "--channel", unique_channel,
        parse_json=True,
    )
    item_hash = result["item_hash"]
    assert item_hash

    # Give the CCN a moment to process
    time.sleep(2)

    list_result = aleph_cli(
        "post", "list",
        "--channels", unique_channel,
        parse_json=True,
    )
    hashes = [p["item_hash"] for p in list_result["posts"]]
    assert item_hash in hashes, f"Created post {item_hash} should appear in channel listing"


def test_post_amend(aleph_cli, unique_channel):
    """Create a post, amend it, verify updated content."""
    original = '{"body": "original"}'
    result = aleph_cli(
        "post", "create",
        "--type", "test-post",
        "--content", original,
        "--channel", unique_channel,
        parse_json=True,
    )
    item_hash = result["item_hash"]

    time.sleep(2)

    updated = '{"body": "amended"}'
    aleph_cli(
        "post", "amend",
        "--ref", item_hash,
        "--content", updated,
        parse_json=True,
    )

    time.sleep(2)

    list_result = aleph_cli(
        "post", "list",
        "--channels", unique_channel,
        parse_json=True,
    )
    # Find the post and check its content was amended
    matching = [p for p in list_result["posts"] if p.get("original_item_hash") == item_hash or p["item_hash"] == item_hash]
    assert len(matching) > 0, "Amended post should appear in listing"
    latest = matching[0]
    assert latest["content"]["body"] == "amended", "Post content should reflect the amendment"
```

- [ ] **Step 2: Commit**

```bash
git add tests/test_posts.py
git commit -m "feat: add post create/list/amend integration tests"
```

---

### Task 7: Aggregate Integration Tests

**Files:**
- Create: `tests/test_aggregates.py`

- [ ] **Step 1: Write aggregate tests**

```python
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
    result = aleph_cli(
        "message", "list",
        "--message-types", "AGGREGATE",
        parse_json=True,
    )
    # Verify our aggregate key appears in the results
    found = False
    for msg in result.get("messages", []):
        if msg.get("content", {}).get("key") == key:
            found = True
            assert msg["content"]["content"]["score"] == 42
            assert msg["content"]["content"]["name"] == "integration-test"
            break
    assert found, f"Aggregate with key '{key}' should appear in message listing"
```

- [ ] **Step 2: Commit**

```bash
git add tests/test_aggregates.py
git commit -m "feat: add aggregate create/read integration test"
```

---

### Task 8: Message Integration Tests

**Files:**
- Create: `tests/test_messages.py`

- [ ] **Step 1: Write message tests**

```python
import time


def test_message_list(aleph_cli, unique_channel):
    """Create a message and verify it appears in the message listing."""
    aleph_cli(
        "post", "create",
        "--type", "test-msg",
        "--content", '{"purpose": "list-test"}',
        "--channel", unique_channel,
        parse_json=True,
    )

    time.sleep(2)

    result = aleph_cli(
        "message", "list",
        "--channels", unique_channel,
        parse_json=True,
    )
    messages = result.get("messages", [])
    assert len(messages) > 0, "Should find at least one message in the channel"
    assert messages[0].get("channel") == unique_channel


def test_message_forget(aleph_cli, unique_channel):
    """Create a post, forget it, verify it disappears."""
    result = aleph_cli(
        "post", "create",
        "--type", "test-forget",
        "--content", '{"ephemeral": true}',
        "--channel", unique_channel,
        parse_json=True,
    )
    item_hash = result["item_hash"]

    time.sleep(2)

    # Forget the message
    aleph_cli("message", "forget", item_hash, parse_json=True)

    time.sleep(2)

    # Verify it's gone from listings
    list_result = aleph_cli(
        "message", "list",
        "--channels", unique_channel,
        parse_json=True,
    )
    remaining_hashes = [m["item_hash"] for m in list_result.get("messages", [])]
    assert item_hash not in remaining_hashes, "Forgotten message should not appear in listing"
```

- [ ] **Step 2: Commit**

```bash
git add tests/test_messages.py
git commit -m "feat: add message list/forget integration tests"
```

---

### Task 9: Deploy Workflow (GitHub Actions)

**Files:**
- Create: `.github/workflows/deploy.yml`

- [ ] **Step 1: Create deploy workflow**

```yaml
name: Deploy Testnet

on:
  push:
    branches: [main]

jobs:
  deploy:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4

      - name: Set up Python
        uses: actions/setup-python@v5
        with:
          python-version: "3.12"

      - name: Install pyyaml
        run: pip install pyyaml

      - name: Set up SSH key
        run: |
          mkdir -p ~/.ssh
          echo "${{ secrets.TESTNET_SSH_PRIVATE_KEY }}" > ~/.ssh/testnet_key
          chmod 600 ~/.ssh/testnet_key

      - name: Deploy
        env:
          TESTNET_HOST: ${{ secrets.TESTNET_HOST }}
          TESTNET_USER: ${{ secrets.TESTNET_USER }}
          SSH_KEY_FILE: ~/.ssh/testnet_key
        run: bash deploy/deploy.sh
```

- [ ] **Step 2: Commit**

```bash
git add .github/workflows/deploy.yml
git commit -m "feat: add deploy-on-merge workflow"
```

---

### Task 10: Nightly Test Workflow (GitHub Actions)

**Files:**
- Create: `.github/workflows/nightly.yml`

- [ ] **Step 1: Create nightly workflow**

```yaml
name: Nightly Integration Tests

on:
  schedule:
    - cron: "0 3 * * *"  # 3 AM UTC daily
  workflow_dispatch: {}   # allow manual trigger

jobs:
  test:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4

      - name: Set up Python
        uses: actions/setup-python@v5
        with:
          python-version: "3.12"

      - name: Install dependencies
        run: pip install pytest pyyaml

      - name: Download Aleph CLI
        run: |
          mkdir -p bin
          CLI_URL=$(python3 -c "
          import yaml
          with open('manifesto.yml') as f:
              m = yaml.safe_load(f)
          print(m['components']['aleph-cli']['url'])
          ")
          curl -fsSL "$CLI_URL" -o bin/aleph
          chmod +x bin/aleph
          echo "$PWD/bin" >> "$GITHUB_PATH"

      - name: Verify CLI works
        run: aleph --version

      - name: Run integration tests
        env:
          ALEPH_TESTNET_CCN_URL: ${{ secrets.TESTNET_CCN_URL }}
          ALEPH_TESTNET_PRIVATE_KEY: ${{ secrets.TESTNET_PRIVATE_KEY }}
        run: pytest --junitxml=results.xml -v

      - name: Upload test results
        if: always()
        uses: actions/upload-artifact@v4
        with:
          name: test-results
          path: results.xml
```

- [ ] **Step 2: Commit**

```bash
git add .github/workflows/nightly.yml
git commit -m "feat: add nightly integration test workflow"
```

---

### Task 11: Final Validation

- [ ] **Step 1: Verify all files are committed and the repo is clean**

```bash
git status
```

Expected: clean working tree.

- [ ] **Step 2: Verify the test suite collects all expected tests**

```bash
pytest --collect-only 2>&1
```

Expected: collects `test_file_upload_and_download`, `test_file_upload_with_ref`, `test_post_create_and_list`, `test_post_amend`, `test_aggregate_create_and_read`, `test_message_list`, `test_message_forget` (7 tests total).

- [ ] **Step 3: Review the full file tree**

```bash
find . -not -path './.git/*' -type f | sort
```

Expected:
```
./.github/workflows/deploy.yml
./.github/workflows/nightly.yml
./deploy/001-update-ipfs-config.sh
./deploy/config.yml.tpl
./deploy/deploy.sh
./deploy/docker-compose.yml
./docs/superpowers/plans/2026-03-20-aleph-testnets-phase1.md
./docs/superpowers/specs/2026-03-20-aleph-testnets-design.md
./manifesto.yml
./pyproject.toml
./tests/__init__.py
./tests/conftest.py
./tests/test_aggregates.py
./tests/test_files.py
./tests/test_messages.py
./tests/test_posts.py
```
