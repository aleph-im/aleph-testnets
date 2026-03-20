#!/bin/sh
# Minimal IPFS config for testnet use

echo "Updating IPFS config for testnet..."

ipfs config Reprovider.Strategy 'pinned'
ipfs config Routing.Type "dhtserver"
ipfs config Datastore.StorageMax '5GB'
ipfs config Datastore.GCPeriod '12h'
ipfs config Bootstrap --json '[]'

echo "IPFS config updated."
