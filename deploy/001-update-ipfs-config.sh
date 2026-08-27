#!/bin/sh
# Minimal IPFS config for testnet use

echo "Updating IPFS config for testnet..."

# Kubo v0.43.0 (pyaleph 0.11.0) refuses to start on the pre-0.42 Reprovider.* keys.
ipfs config Provide.Strategy 'pinned'
ipfs config Routing.Type "dhtserver"
ipfs config Datastore.StorageMax '5GB'
ipfs config Datastore.GCPeriod '12h'
ipfs config Bootstrap --json '[]'

echo "IPFS config updated."
