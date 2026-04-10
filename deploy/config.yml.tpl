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
  max_file_size: 1073741824

ipfs:
  alive_topic: ALEPH_TESTNET_ALIVE
  enabled: true
  host: ipfs
  port: 5001
  gateway_port: 8080
  peers: []

aleph:
  queue_topic: ALEPH_TESTNET_TEST
  balances:
    addresses:
      - "0x70997970C51812dc3A010C7d01b50e0d17dc79C8"  # Account #1 (nodestatus)
  credit_balances:
    addresses:
      - "0xf39Fd6e51aad88F6F4ce6aB8827279cffFb92266"
    post_types:
      - aleph_credit_distribution
      - aleph_credit_transfer
      - aleph_credit_expense
    channels:
      - ALEPH_TESTNET_CREDIT

p2p:
  daemon_host: p2p-service
  http_port: 4024
  port: 4025
  control_port: 4030
  reconnect_delay: 60
  peers: []
  topics:
    - ALEPH_TESTNET_ALIVE
    - ALEPH_TESTNET_TEST
  alive_topic: ALEPH_TESTNET_ALIVE

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
