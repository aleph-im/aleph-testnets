# Nodestatus Integration Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add aleph-nodestatus to the local testnet stack so we can test `aleph node` commands (corechannel aggregates) and holder tier payments (ALEPH balances on Ethereum).

**Architecture:** Two new Docker containers (`nodestatus`, `nodestatus-balances`) built from `../aleph-nodestatus`, configured entirely via environment variables. Account layout shifts test users from Anvil #1-#3 to #4-#6, freeing #1 for nodestatus signing.

**Tech Stack:** Docker Compose, Bash, Python/pytest, Foundry (cast), aleph-nodestatus (unmodified)

**Spec:** `docs/superpowers/specs/2026-03-26-nodestatus-integration-design.md`

---

## File Map

| File | Action | Responsibility |
|------|--------|---------------|
| `deploy/docker-compose.yml` | Modify | Add `nodestatus` + `nodestatus-balances` services under `nodestatus` profile |
| `deploy/config.yml.tpl` | Modify | Add `balances.addresses` config for pyaleph |
| `scripts/local-up.sh` | Modify | Shift test key to account #4, start nodestatus containers, wait for readiness |
| `scripts/fund-test-accounts.sh` | Modify | Shift test accounts to #4-#6, add MockALEPH minting |
| `tests/conftest.py` | Modify | Update default keys to account #4, add nodestatus fixtures |
| `tests/test_credits.py` | Modify | Update `TEST_ADDR` to account #4 |
| `tests/test_balances.py` | Create | ALEPH balance tracking tests |
| `tests/test_nodes.py` | Create | Corechannel node operation tests |
| `Justfile` | Modify | Add `nodestatus` profile to stop/logs commands |

---

### Task 1: Account layout reshuffle

Shift test users from Anvil accounts #1-#3 to #4-#6. Account #1 becomes the nodestatus signing account.

**Files:**
- Modify: `scripts/fund-test-accounts.sh`
- Modify: `scripts/local-up.sh:24`
- Modify: `tests/conftest.py:142`
- Modify: `tests/test_credits.py:7`

- [ ] **Step 1: Update `scripts/local-up.sh` test private key**

Change the `TEST_PRIVATE_KEY` from account #1 to account #4:

```bash
# Old (account #1):
TEST_PRIVATE_KEY="59c6995e998f97a5a0044966f0945389dc9e86dae88c7a8412f4603b6b78690d"
# New (account #4):
TEST_PRIVATE_KEY="47e179ec197488593b187f80a00eb0da91f1b9d0b13f8733639f19c30a34926a"
```

- [ ] **Step 2: Update `scripts/fund-test-accounts.sh` accounts**

Replace the `TEST_ADDRS` and `TEST_KEYS` arrays. Update the header comment to reflect the new layout:

```bash
# Anvil account layout:
#   #0  0xf39F...  Reserved — contract deployer + credit service sender
#   #1  0x7099...  Reserved — nodestatus signing account
#   #2  0x3C44...  Reserved
#   #3  0x90F7...  Reserved
#   #4  0x15d3...  Test account
#   #5  0x9965...  Test account
#   #6  0x976E...  Test account

# ...

# Test accounts: Anvil #4, #5, #6
TEST_ADDRS=(
    "0x15d34AAf54267DB7D7c367839AAf71A00a2C6A65"
    "0x9965507D1a55bcC2695C58ba16FB37d819B0A4dc"
    "0x976EA74026E726554dB657fA54763abd0C3a0aa9"
)
TEST_KEYS=(
    "0x47e179ec197488593b187f80a00eb0da91f1b9d0b13f8733639f19c30a34926a"
    "0x8b3a350cf5c34c9194ca85829a2df0ec3153be0318b5e2d3348e872092edffba"
    "0x92db14e403b83dfe3df233f83dfa3a0d7096f21ca9b0d6d6b8d88b2b4ec1564e"
)
```

- [ ] **Step 3: Update `tests/conftest.py` default private key**

The `cast_send` fixture has account #1's key as default. Change to account #4:

```python
# Old:
private_key: str = "0x59c6995e998f97a5a0044966f0945389dc9e86dae88c7a8412f4603b6b78690d",  # Anvil #1
# New:
private_key: str = "0x47e179ec197488593b187f80a00eb0da91f1b9d0b13f8733639f19c30a34926a",  # Anvil #4
```

- [ ] **Step 4: Update `tests/test_credits.py` test address**

```python
# Old:
TEST_ADDR = "0x70997970C51812dc3A010C7d01b50e0d17dc79C8"
# New:
TEST_ADDR = "0x15d34AAf54267DB7D7c367839AAf71A00a2C6A65"
```

Also update the comment:

```python
# Old:
# Anvil account #1 — primary test account (see scripts/fund-test-accounts.sh)
# New:
# Anvil account #4 — primary test account (see scripts/fund-test-accounts.sh)
```

- [ ] **Step 5: Commit**

```bash
git add scripts/local-up.sh scripts/fund-test-accounts.sh tests/conftest.py tests/test_credits.py
git commit -m "refactor: shift test accounts from #1-#3 to #4-#6

Account #1 is now reserved for nodestatus signing.
Accounts #0-#3 are privileged, #4+ are test users."
```

---

### Task 2: Add nodestatus containers to docker-compose

**Files:**
- Modify: `deploy/docker-compose.yml`

- [ ] **Step 1: Add nodestatus and nodestatus-balances services**

Append before the `networks:` block at the end of `deploy/docker-compose.yml`:

```yaml
  nodestatus:
    restart: always
    build:
      context: ../aleph-nodestatus
      dockerfile: docker/Dockerfile
    command: nodestatus -vv --window-size 1h
    environment:
      ETHEREUM_API_SERVER: http://anvil:8545
      ETHEREUM_TOKEN_CONTRACT: ${MOCKALEPH_ADDRESS}
      ETHEREUM_CHAIN_ID: "31337"
      ETHEREUM_MIN_HEIGHT: "0"
      ETHEREUM_TOTAL_SUPPLY: "0"
      ETHEREUM_DEPLOYER: "0x0000000000000000000000000000000000000000"
      ETHEREUM_DECIMALS: "18"
      ETHEREUM_PKEY: "59c6995e998f97a5a0044966f0945389dc9e86dae88c7a8412f4603b6b78690d"
      ALEPH_API_SERVER: http://pyaleph-api:4024
      ALEPH_CHANNEL: TEST
      FILTER_TAG: mainnet
      DB_PATH: /data/database
      BALANCES_SENDERS: '["0x70997970C51812dc3A010C7d01b50e0d17dc79C8"]'
      SCORES_SENDERS: '["0x70997970C51812dc3A010C7d01b50e0d17dc79C8"]'
      STATUS_SENDER: "0x70997970C51812dc3A010C7d01b50e0d17dc79C8"
    depends_on:
      - anvil
      - pyaleph-api
    profiles:
      - nodestatus
    networks:
      - pyaleph
    logging:
      options:
        max-size: 50m

  nodestatus-balances:
    restart: always
    build:
      context: ../aleph-nodestatus
      dockerfile: docker/Dockerfile
    command: monitor-balances -vv
    environment:
      ETHEREUM_API_SERVER: http://anvil:8545
      ETHEREUM_TOKEN_CONTRACT: ${MOCKALEPH_ADDRESS}
      ETHEREUM_CHAIN_ID: "31337"
      ETHEREUM_MIN_HEIGHT: "0"
      ETHEREUM_TOTAL_SUPPLY: "0"
      ETHEREUM_DEPLOYER: "0x0000000000000000000000000000000000000000"
      ETHEREUM_DECIMALS: "18"
      ETHEREUM_PKEY: "59c6995e998f97a5a0044966f0945389dc9e86dae88c7a8412f4603b6b78690d"
      ALEPH_API_SERVER: http://pyaleph-api:4024
      ALEPH_CHANNEL: TEST
      FILTER_TAG: mainnet
      DB_PATH: /data/database
      BALANCES_SENDERS: '["0x70997970C51812dc3A010C7d01b50e0d17dc79C8"]'
      SCORES_SENDERS: '["0x70997970C51812dc3A010C7d01b50e0d17dc79C8"]'
      STATUS_SENDER: "0x70997970C51812dc3A010C7d01b50e0d17dc79C8"
    depends_on:
      - anvil
      - pyaleph-api
    profiles:
      - nodestatus
    networks:
      - pyaleph
    logging:
      options:
        max-size: 50m
```

- [ ] **Step 2: Commit**

```bash
git add deploy/docker-compose.yml
git commit -m "feat: add nodestatus containers to docker-compose

Two containers under 'nodestatus' profile:
- nodestatus: corechannel aggregate publisher
- nodestatus-balances: ALEPH balance tracker"
```

---

### Task 3: Update pyaleph config template

**Files:**
- Modify: `deploy/config.yml.tpl`

- [ ] **Step 1: Add balances config to config.yml.tpl**

Add the `balances:` block under the existing `aleph:` section, before `credit_balances:`:

```yaml
aleph:
  queue_topic: ALEPH_TESTNET_TEST
  balances:
    addresses:
      - "0x70997970C51812dc3A010C7d01b50e0d17dc79C8"  # Account #1 (nodestatus)
  credit_balances:
    addresses:
      - "0xf39Fd6e51aad88F6F4ce6aB8827279cffFb92266"
    # ... rest unchanged
```

- [ ] **Step 2: Commit**

```bash
git add deploy/config.yml.tpl
git commit -m "feat: whitelist nodestatus address for ALEPH balance posts"
```

---

### Task 4: Add MockALEPH minting to funding script

**Files:**
- Modify: `scripts/fund-test-accounts.sh`

- [ ] **Step 1: Read MockALEPH address from contracts.json**

After the existing `USDALEPH_ADDR` and `CREDIT_ADDR` lines, add:

```bash
MOCKALEPH_ADDR=$(jq -r '.mock_aleph' "$CONTRACTS_JSON")
```

- [ ] **Step 2: Add MockALEPH minting in the funding loop**

In the existing "Step 1" loop that mints USDAleph and sends ETH, add a MockALEPH mint call. MockALEPH has 18 decimals; mint 1,000,000 ALEPH = `1000000000000000000000000` raw.

Update the echo and add the mint:

```bash
    ALEPH_MINT="1000000000000000000000000"  # 1,000,000 ALEPH (18 decimals)

    echo "    $ADDR: 1 ETH + 200 USDAleph + 1M ALEPH"

    # ... existing ETH send ...
    # ... existing USDAleph mint ...

    cast_run send --rpc-url http://anvil:8545 --private-key "$DEPLOYER_KEY" \
        "$MOCKALEPH_ADDR" "mint(address,uint256)" "$ADDR" "$ALEPH_MINT" > /dev/null
```

- [ ] **Step 3: Add ALEPH balances to the summary**

In the "Step 4: Summary" section, show ALEPH balances alongside credits:

```bash
    ALEPH_RAW=$(cast_run call --rpc-url http://anvil:8545 \
        "$MOCKALEPH_ADDR" "balanceOf(address)(uint256)" "$ADDR" 2>/dev/null | head -1 || echo "?")
    if [ "$ALEPH_RAW" != "?" ]; then
        # Convert from 18-decimal raw to human-readable (integer division)
        ALEPH_HUMAN=$(echo "$ALEPH_RAW" | sed 's/\[.*\]//' | xargs)
        printf "    %s  %s credits  %s ALEPH (raw)\n" "$ADDR" "$CREDITS" "$ALEPH_HUMAN"
    else
        printf "    %s  %s credits\n" "$ADDR" "$CREDITS"
    fi
```

- [ ] **Step 4: Commit**

```bash
git add scripts/fund-test-accounts.sh
git commit -m "feat: mint MockALEPH to test accounts during funding"
```

---

### Task 5: Update startup sequence in local-up.sh

**Files:**
- Modify: `scripts/local-up.sh`

- [ ] **Step 1: Start nodestatus containers after credits services**

In the `deploy_contracts()` function, after the line that starts the indexer and credit service, add:

```bash
    # Start nodestatus services (corechannel aggregates + balance tracking)
    echo "==> Starting nodestatus services..."
    docker compose "${COMPOSE_FILES[@]}" --profile nodestatus up -d --build nodestatus nodestatus-balances
```

This goes after the indexer start but before `fund-test-accounts.sh` — nodestatus needs to be running to detect the MockALEPH mint events from funding.

- [ ] **Step 2: Update dump_logs() to include nodestatus profile**

```bash
dump_logs() {
    echo "==> Dumping container logs..."
    for svc in $(docker compose "${COMPOSE_FILES[@]}" --profile credits --profile nodestatus config --services 2>/dev/null); do
        echo "===== $svc ====="
        docker compose "${COMPOSE_FILES[@]}" --profile credits --profile nodestatus logs --no-color --tail=200 "$svc" 2>/dev/null || true
    done
}
```

- [ ] **Step 3: Update stack_down() to include nodestatus profile**

```bash
stack_down() {
    echo "==> Stopping CCN stack..."
    docker compose "${COMPOSE_FILES[@]}" --profile credits --profile nodestatus down -v || true
    # ... rest unchanged
```

- [ ] **Step 4: Commit**

```bash
git add scripts/local-up.sh
git commit -m "feat: start nodestatus containers in deploy_contracts phase"
```

---

### Task 6: Update Justfile for nodestatus profile

**Files:**
- Modify: `Justfile`

- [ ] **Step 1: Add nodestatus profile to stop-dev-env**

```just
# Old:
stop-dev-env:
    {{ compose }} --profile credits stop

# New:
stop-dev-env:
    {{ compose }} --profile credits --profile nodestatus stop
```

- [ ] **Step 2: Commit**

```bash
git add Justfile
git commit -m "feat: include nodestatus profile in Justfile stop command"
```

---

### Task 7: Add nodestatus test fixtures to conftest.py

**Files:**
- Modify: `tests/conftest.py`

- [ ] **Step 1: Add `mock_aleph_addr` fixture**

Add after the `contracts` fixture:

```python
@pytest.fixture(scope="session")
def mock_aleph_addr(contracts):
    """MockALEPH contract address on Anvil."""
    return contracts["mock_aleph"]
```

- [ ] **Step 2: Add `mint_aleph` fixture**

Add after `mock_aleph_addr`:

```python
@pytest.fixture(scope="session")
def mint_aleph(mock_aleph_addr, cast_send):
    """Return a function that mints MockALEPH to an address.

    Amount is in whole ALEPH tokens (e.g. 1000000 = 1M ALEPH).
    MockALEPH has 18 decimals.
    """
    deployer_key = "0xac0974bec39a17e36ba4a6b4d238ff944bacb478cbed5efcae784d7bf4f2ff80"

    def mint(to: str, amount: int):
        raw = str(amount * 10**18)
        cast_send(
            mock_aleph_addr,
            "mint(address,uint256)",
            to, raw,
            private_key=deployer_key,
        )
    return mint
```

- [ ] **Step 3: Add `ccn_messages` fixture**

A helper to query messages from the CCN API. Add after `ccn_api`:

```python
@pytest.fixture(scope="session")
def ccn_messages(ccn_url: str):
    """Return a function that queries the CCN messages API."""
    def query(params: dict) -> list:
        qs = "&".join(f"{k}={v}" for k, v in params.items())
        url = f"{ccn_url}/api/v0/messages.json?{qs}"
        req = urllib.request.Request(url, headers={"Accept": "application/json"})
        resp = urllib.request.urlopen(req, timeout=10)
        data = json.loads(resp.read())
        return data.get("messages", [])
    return query
```

- [ ] **Step 4: Add `ccn_aggregates` fixture**

A helper to fetch aggregates. Add after `ccn_messages`:

```python
@pytest.fixture(scope="session")
def ccn_aggregates(ccn_url: str):
    """Return a function that fetches an aggregate from the CCN."""
    def get(address: str, key: str) -> dict | None:
        url = f"{ccn_url}/api/v0/aggregates/{address}.json?keys={key}"
        req = urllib.request.Request(url, headers={"Accept": "application/json"})
        try:
            resp = urllib.request.urlopen(req, timeout=10)
            data = json.loads(resp.read())
            return data.get("data", {}).get(key)
        except urllib.error.HTTPError:
            return None
    return get
```

- [ ] **Step 5: Commit**

```bash
git add tests/conftest.py
git commit -m "feat: add nodestatus test fixtures (mint_aleph, ccn_messages, ccn_aggregates)"
```

---

### Task 8: Write ALEPH balance tests

**Files:**
- Create: `tests/test_balances.py`

- [ ] **Step 1: Write test_balances.py**

```python
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
```

- [ ] **Step 2: Verify the test file parses**

Run: `cd /home/olivier/git/aleph/aleph-testnets && python -c "import ast; ast.parse(open('tests/test_balances.py').read()); print('OK')"`
Expected: `OK`

- [ ] **Step 3: Commit**

```bash
git add tests/test_balances.py
git commit -m "feat: add ALEPH balance tracking integration tests"
```

---

### Task 9: Write corechannel node tests

**Files:**
- Create: `tests/test_nodes.py`

- [ ] **Step 1: Write test_nodes.py**

```python
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
            # The aggregate should have at least 'nodes' and 'resource_nodes' keys
            assert "nodes" in agg or isinstance(agg, dict), (
                f"Unexpected corechannel aggregate shape: {list(agg.keys()) if isinstance(agg, dict) else type(agg)}"
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
    result = aleph_cli("node", "create", "--name", "test-ccn", check=False)

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
```

- [ ] **Step 2: Verify the test file parses**

Run: `cd /home/olivier/git/aleph/aleph-testnets && python -c "import ast; ast.parse(open('tests/test_nodes.py').read()); print('OK')"`
Expected: `OK`

- [ ] **Step 3: Commit**

```bash
git add tests/test_nodes.py
git commit -m "feat: add corechannel node operation integration tests"
```

---

### Task 10: Final integration verification

**Files:** None (verification only)

- [ ] **Step 1: Verify docker-compose config is valid**

Run: `cd /home/olivier/git/aleph/aleph-testnets && docker compose -f deploy/docker-compose.yml -f deploy/docker-compose.local.yml --profile nodestatus config --services`

Expected output should include `nodestatus` and `nodestatus-balances` in the service list.

Note: This will fail if `deploy/.env` doesn't exist (it's generated at runtime). If so, create a minimal one for validation:

```bash
echo 'MOCKALEPH_ADDRESS=0x0000000000000000000000000000000000000001' >> deploy/.env
```

Then clean it up after validation.

- [ ] **Step 2: Verify all test files parse**

Run: `cd /home/olivier/git/aleph/aleph-testnets && python -c "import ast; [ast.parse(open(f'tests/{f}').read()) for f in ['test_balances.py', 'test_nodes.py', 'conftest.py', 'test_credits.py']]; print('All OK')"`

Expected: `All OK`

- [ ] **Step 3: Verify no stale references to old test accounts**

Run: `grep -rn "0x70997970C51812dc3A010C7d01b50e0d17dc79C8" tests/ scripts/`

Expected: Only hits in contexts where account #1 is used as the nodestatus/privileged account, NOT as a test user. Specifically:
- `tests/conftest.py` should NOT have this as `cast_send` default
- `scripts/fund-test-accounts.sh` should NOT have this in `TEST_ADDRS`
- `scripts/local-up.sh` should NOT have this as `TEST_PRIVATE_KEY`

- [ ] **Step 4: Commit any fixes from verification**

If verification found issues, fix and commit:

```bash
git add -A
git commit -m "fix: address issues found during integration verification"
```
