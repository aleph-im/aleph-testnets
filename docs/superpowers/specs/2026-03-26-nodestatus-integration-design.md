# Nodestatus Service Integration

Integrate `aleph-nodestatus` into the local testnet stack to enable:
1. **Corechannel node aggregates** -- required to test `aleph node` CLI commands (create, stake, link CRNs)
2. **ALEPH balances on Ethereum** -- required to test holder tier payments

## Approach

Run nodestatus as-is from `../aleph-nodestatus` with zero source code changes. All adaptation is via environment variables (Pydantic `BaseSettings`). Two containers under a `nodestatus` docker-compose profile.

## Architecture

```
                          +------------------+
                          |      Anvil       |
                          |  (MockALEPH ERC20|
                          |   + USDAleph)    |
                          +--------+---------+
                                   |
                    +--------------+--------------+
                    |                             |
           +-------v--------+           +--------v--------+
           |   nodestatus   |           | nodestatus-     |
           |                |           | balances        |
           | Reads:         |           |                 |
           | - ERC20 events |           | Reads:          |
           | - corechan-op  |           | - ERC20 events  |
           |   posts        |           |                 |
           | - score posts  |           | Publishes:      |
           |                |           | - balances-     |
           | Publishes:     |           |   update posts  |
           | - corechannel  |           |   to CCN        |
           |   aggregate    |           |                 |
           +-------+--------+           +--------+--------+
                    |                             |
                    +-------------+---------------+
                                  |
                          +-------v--------+
                          |  pyaleph-api   |
                          |  (CCN @ :4024) |
                          +----------------+
```

### Containers

| Container | Command | Purpose | Profile |
|-----------|---------|---------|---------|
| `nodestatus` | `nodestatus -vv --window-size 1h` | Process node operations, publish `corechannel` aggregate | `nodestatus` |
| `nodestatus-balances` | `monitor-balances -vv` | Watch MockALEPH transfers, publish `balances-update` posts | `nodestatus` |

### Docker Image

Built from `../aleph-nodestatus` using its existing `docker/Dockerfile` (Python 3.9, `setup.py install`). The build context needs to be the aleph-nodestatus repo root (the Dockerfile ADDs `../`).

In docker-compose:

```yaml
nodestatus:
  build:
    context: ../../aleph-nodestatus
    dockerfile: docker/Dockerfile
  ...

nodestatus-balances:
  build:
    context: ../../aleph-nodestatus
    dockerfile: docker/Dockerfile
  ...
```

Both containers share the same image, different entrypoint commands.

## Account Layout

Shift from current layout (accounts #1-#3 as test users) to a cleaner separation:

| Anvil Account | Address | Private Key | Role |
|---------------|---------|-------------|------|
| #0 | `0xf39Fd6e51aad88F6F4ce6aB8827279cffFb92266` | `0xac0974bec39a17e36ba4a6b4d238ff944bacb478cbed5efcae784d7bf4f2ff80` | Deployer + credit service sender |
| #1 | `0x70997970C51812dc3A010C7d01b50e0d17dc79C8` | `0x59c6995e998f97a5a0044966f0945389dc9e86dae88c7a8412f4603b6b78690d` | Nodestatus signing account |
| #2 | `0x3C44CdDdB6a900fa2b585dd299e03d12FA4293BC` | `0x5de4111afa1a4b94908f83103eb1f1706367c2e68ca870fc3fb9a804cdab365a` | Reserved |
| #3 | `0x90F79bf6EB2c4f870365E785982E1f101E93b906` | `0x7c852118294e51e653712a81e05800f419141751be58f605c371e15141b007a6` | Reserved |
| #4 | `0x15d34AAf54267DB7D7c367839AAf71A00a2C6A65` | `0x47e179ec197488593b187f80a00eb0da91f1b9d0b13f8733639f19c30a34926a` | Test user |
| #5 | `0x9965507D1a55bcC2695C58ba16FB37d819B0A4dc` | `0x8b3a350cf5c34c9194ca85829a2df0ec3153be0318b5e2d3348e872092edffba` | Test user |
| #6 | `0x976EA74026E726554dB657fA54763abd0C3a0aa9` | `0x92db14e403b83dfe3df233f83dfa3a0d7096f21ca9b0d6d6b8d88b2b4ec1564e` | Test user |

### Files affected by reshuffle

- `scripts/fund-test-accounts.sh` -- test addresses shift to #4-#6
- `scripts/local-up.sh` -- `TEST_PRIVATE_KEY` becomes account #4's key
- `deploy/config.yml.tpl` -- add `balances` config (see below)
- `tests/conftest.py` -- update default private key

## Configuration

### Nodestatus environment variables

Both containers share the same env block (different commands):

```yaml
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
```

Key decisions:
- `ETHEREUM_TOTAL_SUPPLY=0` + `ETHEREUM_DEPLOYER=0x0`: MockALEPH uses `mint()`, starting from 0 supply. Transfer events from `0x0` correctly build up balances.
- `FILTER_TAG=mainnet`: Matches what the aleph CLI hardcodes in `corechan-operation` posts.
- `ETHEREUM_PKEY`: Account #1's key (without `0x` prefix -- nodestatus expects raw hex).
- `BALANCES_SENDERS`/`SCORES_SENDERS`: Whitelist account #1 so the service trusts its own published messages on re-read.

### pyaleph configuration (config.yml.tpl)

Add ALEPH balance whitelisting under the `aleph:` section:

```yaml
aleph:
  queue_topic: ALEPH_TESTNET_TEST
  balances:
    addresses:
      - "0x70997970C51812dc3A010C7d01b50e0d17dc79C8"  # Account #1 (nodestatus)
  credit_balances:
    addresses:
      - "0xf39Fd6e51aad88F6F4ce6aB8827279cffFb92266"  # Account #0 (credit service)
    post_types:
      - aleph_credit_distribution
      - aleph_credit_transfer
      - aleph_credit_expense
    channels:
      - ALEPH_TESTNET_CREDIT
```

## Token Funding

Extend `fund-test-accounts.sh` to also mint MockALEPH to test accounts:

- **Test accounts (#4-#6)**: 1,000,000 ALEPH each (enough for node creation at 200K threshold + staking)
- MockALEPH has 18 decimals: `1000000 * 10^18 = 1000000000000000000000000`
- Uses deployer (account #0) to call `MockALEPH.mint(address, uint256)`

The `nodestatus-balances` container will detect these mint Transfer events and publish `balances-update` posts to the CCN.

## Startup Sequence

Extends the existing `deploy_contracts` phase in `local-up.sh`:

1. Deploy contracts on Anvil (existing)
2. Start indexer + credit-service (existing)
3. **Start nodestatus + nodestatus-balances** (new)
4. Fund test accounts with USDAleph (existing) **+ MockALEPH** (new)
5. Wait for credit balances (existing)
6. **Wait for ALEPH balances** on CCN (new) -- poll until `balances-update` posts appear

## Integration Tests

### test_nodes.py -- Corechannel node operations

1. **test_corechannel_aggregate_exists**: After startup, verify nodestatus has published a `corechannel` aggregate to the CCN (even if empty).

2. **test_create_node**: Mint enough ALEPH to a test account, run `aleph node create`, wait for nodestatus to process, verify the node appears in the `corechannel` aggregate.

3. **test_stake_on_node**: After creating a node, stake ALEPH from another account, verify the staker appears and total staked updates.

### test_balances.py -- ALEPH balance tracking

1. **test_aleph_balance_funded**: Verify test accounts show ALEPH balances on the CCN after funding (sanity check that nodestatus-balances is working).

2. **test_aleph_balance_transfer**: Transfer MockALEPH between accounts on Anvil, wait for `monitor-balances` to publish an update, verify the new balances appear.

### Test helpers needed in conftest.py

- `nodestatus_ready()` fixture: Wait for the `corechannel` aggregate to exist on the CCN
- `mint_aleph(address, amount)` fixture: Mint MockALEPH via cast
- Query helper to fetch the `corechannel` aggregate from the CCN API

## ABI Compatibility

The bundled `ALEPHERC20.json` in nodestatus uses `_from`/`_to`/`_value` parameter names. MockALEPH (OpenZeppelin) uses `from`/`to`/`value`. This is not an issue: web3 matches Transfer events by topic hash (`keccak256("Transfer(address,address,uint256)")`), which is identical regardless of parameter names. The decoded args dict uses names from the ABI we supply, so the code's `args["_from"]` works correctly.

## What's NOT in scope

- Distribution commands (`nodestatus-distribute`, `nodestatus-distribute-credits`) -- not needed for the two target use cases
- Sablier, Solana, multi-chain indexer monitors -- irrelevant for local testnet
- Score publishing -- would require a separate scoring service; nodestatus will run fine without scores (nodes just get score 0)
- CRN testing -- requires actual CRN deployment (Phase 3)
