#!/usr/bin/env bash
# Provision and configure CRN (Compute Resource Node) instances on DigitalOcean.
#
# Usage:
#   ./scripts/crn-up.sh                    # full run: provision → install → register
#   ./scripts/crn-up.sh --provision        # create DO droplet, wait for SSH
#   ./scripts/crn-up.sh --install          # install aleph-vm .deb, configure, start
#   ./scripts/crn-up.sh --register         # register CRN in corechannel aggregate
#   ./scripts/crn-up.sh --status           # check CRN supervisor status
#   ./scripts/crn-up.sh --destroy          # delete droplet and clean state
#
# Environment variables:
#   CCN_HOST            IP/hostname of the CCN (required for --install, --register)
#   SSH_KEY_FILE        Path to SSH private key for droplet access (optional, defaults to ~/.ssh/id_ed25519)
#   DO_SSH_KEY_FINGERPRINT  SSH key fingerprint registered with DigitalOcean (required for --provision)
#   DO_REGION           DigitalOcean region (default: ams3)
#   DO_SIZE             Droplet size (default: s-4vcpu-8gb)
#   CRN_COUNT           Number of CRNs to provision (default: 1)
#   ALEPH_VM_VERSION    Override aleph-vm version from manifesto
#   ALLOCATION_TOKEN    Token for CRN /control/allocations auth (default: "allocate-on-testnet")
#
# State is stored in .local/crn/<index>/ (droplet-name, droplet-ip, crn-hash).
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
LOCAL_DIR="$REPO_ROOT/.local"
BIN_DIR="$REPO_ROOT/bin"

# Defaults
SSH_KEY_FILE="${SSH_KEY_FILE:-$HOME/.ssh/id_ed25519}"
DO_REGION="${DO_REGION:-ams3}"
DO_SIZE="${DO_SIZE:-s-4vcpu-8gb}"
CRN_COUNT="${CRN_COUNT:-1}"
ALLOCATION_TOKEN="${ALLOCATION_TOKEN:-allocate-on-testnet}"

# Anvil account #4 — used for CRN registration (has 1M ALEPH from funding)
CRN_OWNER_KEY="0x47e179ec197488593b187f80a00eb0da91f1b9d0b13f8733639f19c30a34926a"
CRN_OWNER_ADDR="0x15d34AAf54267DB7D7c367839AAf71A00a2C6A65"

# Read aleph-vm version from manifesto (or use env override)
read_vm_version() {
    if [ -n "${ALEPH_VM_VERSION:-}" ]; then
        echo "$ALEPH_VM_VERSION"
        return
    fi
    python3 -c "
import yaml
with open('$REPO_ROOT/manifesto.yml') as f:
    m = yaml.safe_load(f)
print(m['components']['aleph-vm']['version'])
"
}

read_vm_connector_image() {
    python3 -c "
import yaml
with open('$REPO_ROOT/manifesto.yml') as f:
    m = yaml.safe_load(f)
c = m['infrastructure']['vm-connector']
print(f'{c[\"image\"]}:{c[\"tag\"]}')
"
}

crn_dir() {
    local idx="$1"
    echo "$LOCAL_DIR/crn/$idx"
}

crn_ip() {
    local idx="$1"
    cat "$(crn_dir "$idx")/droplet-ip"
}

crn_name() {
    local idx="$1"
    cat "$(crn_dir "$idx")/droplet-name"
}

ssh_crn() {
    local idx="$1"; shift
    local ip
    ip=$(crn_ip "$idx")
    ssh -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null \
        -i "$SSH_KEY_FILE" "root@$ip" "$@"
}

scp_to_crn() {
    local idx="$1"; shift
    local ip
    ip=$(crn_ip "$idx")
    scp -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null \
        -i "$SSH_KEY_FILE" "$@" "root@$ip:/tmp/"
}

# ---------------------------------------------------------------------------
# Phase 1: Provision DigitalOcean droplets
# ---------------------------------------------------------------------------
provision() {
    : "${DO_SSH_KEY_FINGERPRINT:?DO_SSH_KEY_FINGERPRINT must be set}"

    for idx in $(seq 0 $((CRN_COUNT - 1))); do
        local dir
        dir=$(crn_dir "$idx")
        mkdir -p "$dir"

        local name="testnets-crn-${idx}-$(date +%s)"
        echo "$name" > "$dir/droplet-name"

        echo "==> Creating droplet $name ..."
        doctl compute droplet create \
            --image debian-12-x64 \
            --size "$DO_SIZE" \
            --region "$DO_REGION" \
            --ssh-keys "$DO_SSH_KEY_FINGERPRINT" \
            --tag-name testnets-crn \
            --wait \
            "$name"

        echo "==> Waiting for IP..."
        local ip=""
        for _ in $(seq 1 30); do
            ip=$(doctl compute droplet get "$name" --format PublicIPv4 --no-header 2>/dev/null || true)
            if [ -n "$ip" ]; then break; fi
            sleep 2
        done
        if [ -z "$ip" ]; then
            echo "ERROR: Could not get IP for $name"
            exit 1
        fi
        echo "$ip" > "$dir/droplet-ip"
        echo "    Droplet $name → $ip"

        echo "==> Waiting for SSH on $ip ..."
        for _ in $(seq 1 30); do
            if ssh-keyscan -H "$ip" >> ~/.ssh/known_hosts 2>/dev/null; then
                break
            fi
            sleep 3
        done

        # Wait for cloud-init to finish
        ssh_crn "$idx" "cloud-init status --wait || true"
    done
    echo "==> All CRN droplets provisioned."
}

# ---------------------------------------------------------------------------
# Phase 2: Install aleph-vm .deb and configure
# ---------------------------------------------------------------------------
install_crn() {
    : "${CCN_HOST:?CCN_HOST must be set (IP/hostname of the CCN)}"

    local version
    version=$(read_vm_version)
    local connector_image
    connector_image=$(read_vm_connector_image)
    local deb_url="https://github.com/aleph-im/aleph-vm/releases/download/${version}/aleph-vm.debian-12.deb"
    local token_hash
    token_hash=$(echo -n "$ALLOCATION_TOKEN" | sha256sum | cut -d' ' -f1)

    echo "==> aleph-vm version: $version"
    echo "    .deb URL: $deb_url"
    echo "    vm-connector: $connector_image"
    echo "    CCN: http://$CCN_HOST:4024"

    for idx in $(seq 0 $((CRN_COUNT - 1))); do
        local ip
        ip=$(crn_ip "$idx")
        echo ""
        echo "==> Installing CRN on $(crn_name "$idx") ($ip) ..."

        # Build supervisor.env
        local env_file
        env_file=$(crn_dir "$idx")/supervisor.env
        cat > "$env_file" <<EOF
ALEPH_VM_SUPERVISOR_HOST=0.0.0.0
ALEPH_VM_DOMAIN_NAME=$ip
ALEPH_VM_API_SERVER=http://$CCN_HOST:4024
ALEPH_VM_OWNER_ADDRESS=$CRN_OWNER_ADDR
ALEPH_VM_ALLOCATION_TOKEN_HASH=$token_hash
EOF

        # If we already have a node hash from registration, include it
        local hash_file
        hash_file=$(crn_dir "$idx")/crn-hash
        if [ -f "$hash_file" ]; then
            echo "ALEPH_VM_NODE_HASH=$(cat "$hash_file")" >> "$env_file"
        fi

        # Copy config
        ssh_crn "$idx" "mkdir -p /etc/aleph-vm"
        scp -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null \
            -i "$SSH_KEY_FILE" "$env_file" "root@$ip:/etc/aleph-vm/supervisor.env"

        # Wait for apt lock
        ssh_crn "$idx" "while fuser /var/lib/apt/lists/lock >/dev/null 2>&1; do sleep 2; done"

        # System update + dependencies
        echo "    Installing system packages..."
        ssh_crn "$idx" "DEBIAN_FRONTEND=noninteractive apt-get -o DPkg::Lock::Timeout=-1 update"
        ssh_crn "$idx" "DEBIAN_FRONTEND=noninteractive apt-get -o DPkg::Lock::Timeout=-1 upgrade -y"
        ssh_crn "$idx" "DEBIAN_FRONTEND=noninteractive apt-get -o DPkg::Lock::Timeout=-1 install -y docker.io apparmor-profiles"

        # Start vm-connector
        echo "    Starting vm-connector..."
        ssh_crn "$idx" "docker pull $connector_image"
        ssh_crn "$idx" "docker run -d -p 127.0.0.1:4021:4021/tcp --restart=always --name vm-connector $connector_image" || \
            ssh_crn "$idx" "docker restart vm-connector" || true

        # Download and install .deb
        echo "    Downloading aleph-vm ${version}..."
        ssh_crn "$idx" "wget -q -O /opt/aleph-vm.deb '$deb_url'"
        echo "    Installing .deb..."
        ssh_crn "$idx" "DEBIAN_FRONTEND=noninteractive apt-get -o DPkg::Lock::Timeout=-1 -o Dpkg::Options::=--force-confold install -y /opt/aleph-vm.deb"

        # Wait for supervisor to be active
        echo "    Waiting for CRN supervisor..."
        local ready=false
        for _ in $(seq 1 30); do
            if ssh_crn "$idx" "systemctl is-active aleph-vm-supervisor.service" 2>/dev/null | grep -q "^active$"; then
                ready=true
                break
            fi
            sleep 5
        done

        if $ready; then
            echo "    CRN supervisor is running on $ip"
        else
            echo "    WARNING: CRN supervisor not active after 150s — check logs"
            ssh_crn "$idx" "systemctl status aleph-vm-supervisor.service" || true
        fi
    done
    echo ""
    echo "==> All CRNs installed."
}

# ---------------------------------------------------------------------------
# Phase 3: Register CRN in corechannel aggregate
# ---------------------------------------------------------------------------
register_crn() {
    : "${CCN_HOST:?CCN_HOST must be set}"

    local aleph_cli="$BIN_DIR/aleph"
    if [ ! -x "$aleph_cli" ]; then
        echo "ERROR: aleph CLI not found at $aleph_cli — run local-up.sh --env first"
        exit 1
    fi

    local ccn_url="http://$CCN_HOST:4024"
    local err_log
    err_log=$(mktemp)

    # Ensure a CCN exists in the corechannel aggregate (required before linking CRNs).
    # The link operation looks up a CCN owned by the same sender account.
    echo "==> Ensuring CCN exists for owner $CRN_OWNER_ADDR ..."
    local ccn_nodes
    ccn_nodes=$(curl -sf "$ccn_url/api/v0/aggregates/$CRN_OWNER_ADDR.json?keys=corechannel" \
        | jq -r '.data.corechannel.nodes // [] | length' 2>/dev/null || echo "0")
    if [ "$ccn_nodes" = "0" ]; then
        echo "    No CCN found — creating one..."
        ALEPH_PRIVATE_KEY="$CRN_OWNER_KEY" "$aleph_cli" \
            --ccn-url "$ccn_url" \
            node create-ccn \
            --name "testnet-ccn" \
            --multiaddress "/ip4/$CCN_HOST/tcp/4025/p2p/testnet" \
            2>"$err_log" || {
            echo "    WARNING: create-ccn failed: $(cat "$err_log")"
        }
        # Wait for nodestatus to process the CCN creation
        echo "    Waiting for CCN to appear in corechannel aggregate..."
        for _ in $(seq 1 24); do
            ccn_nodes=$(curl -sf "$ccn_url/api/v0/aggregates/$CRN_OWNER_ADDR.json?keys=corechannel" \
                | jq -r '.data.corechannel.nodes // [] | length' 2>/dev/null || echo "0")
            if [ "$ccn_nodes" != "0" ]; then
                echo "    CCN is registered."
                break
            fi
            sleep 5
        done
    else
        echo "    CCN already exists."
    fi

    for idx in $(seq 0 $((CRN_COUNT - 1))); do
        local ip
        ip=$(crn_ip "$idx")
        local crn_name_str="testnet-crn-$idx"

        echo "==> Registering CRN $crn_name_str ($ip) ..."

        # Create resource node
        local output
        output=$(ALEPH_PRIVATE_KEY="$CRN_OWNER_KEY" "$aleph_cli" \
            --ccn-url "$ccn_url" --json \
            node create-crn \
            --name "$crn_name_str" \
            --address "http://$ip:4020" 2>"$err_log") || {
            echo "    create-crn failed: $(cat "$err_log")"
            echo "    (This may fail if the account lacks ALEPH balance)"
            continue
        }

        # Extract item_hash from the response
        local crn_hash
        crn_hash=$(echo "$output" | jq -r '.item_hash // empty' 2>/dev/null || true)
        if [ -z "$crn_hash" ]; then
            echo "    Could not extract item_hash from create-crn response."
            echo "    Response: $output"
            echo "    Trying to find node hash from corechannel aggregate..."

            # Fallback: look up the CRN in the aggregate by address
            crn_hash=$(curl -sf "$ccn_url/api/v0/aggregates/$CRN_OWNER_ADDR.json?keys=corechannel" \
                | jq -r ".data.corechannel.resource_nodes[] | select(.address == \"http://$ip:4020\") | .hash" 2>/dev/null || true)
        fi

        if [ -z "$crn_hash" ]; then
            echo "    ERROR: Could not determine CRN hash. Skipping link step."
            continue
        fi

        echo "    CRN hash: $crn_hash"
        echo "$crn_hash" > "$(crn_dir "$idx")/crn-hash"

        # Link CRN to CCN
        echo "    Linking CRN to CCN..."
        ALEPH_PRIVATE_KEY="$CRN_OWNER_KEY" "$aleph_cli" \
            --ccn-url "$ccn_url" \
            node link --crn "$crn_hash" 2>"$err_log" || {
            echo "    WARNING: link failed: $(cat "$err_log")"
        }

        # Update supervisor.env with node hash
        echo "    Setting ALEPH_VM_NODE_HASH on CRN..."
        ssh_crn "$idx" "grep -q ALEPH_VM_NODE_HASH /etc/aleph-vm/supervisor.env && \
            sed -i 's/^ALEPH_VM_NODE_HASH=.*/ALEPH_VM_NODE_HASH=$crn_hash/' /etc/aleph-vm/supervisor.env || \
            echo 'ALEPH_VM_NODE_HASH=$crn_hash' >> /etc/aleph-vm/supervisor.env"
        ssh_crn "$idx" "systemctl restart aleph-vm-supervisor.service"

        echo "    CRN $crn_name_str registered and linked."
    done
    rm -f "$err_log"
    echo ""
    echo "==> CRN registration complete."
}

# ---------------------------------------------------------------------------
# Status check
# ---------------------------------------------------------------------------
status_crn() {
    for idx in $(seq 0 $((CRN_COUNT - 1))); do
        local dir
        dir=$(crn_dir "$idx")
        if [ ! -f "$dir/droplet-ip" ]; then
            echo "CRN $idx: not provisioned"
            continue
        fi
        local ip
        ip=$(crn_ip "$idx")
        local name
        name=$(crn_name "$idx")
        echo "CRN $idx: $name ($ip)"

        # Check supervisor status
        local active
        active=$(ssh_crn "$idx" "systemctl is-active aleph-vm-supervisor.service" 2>/dev/null || echo "unknown")
        echo "    Supervisor: $active"

        # Check /about/usage/system endpoint
        local usage
        usage=$(ssh_crn "$idx" "curl -sf http://localhost:4020/about/usage/system" 2>/dev/null || echo "{}")
        echo "    Usage: $usage"

        # Check registration
        if [ -f "$dir/crn-hash" ]; then
            echo "    Node hash: $(cat "$dir/crn-hash")"
        else
            echo "    Node hash: not registered"
        fi
        echo ""
    done
}

# ---------------------------------------------------------------------------
# Destroy
# ---------------------------------------------------------------------------
destroy() {
    # Iterate over all existing CRN state directories, regardless of CRN_COUNT,
    # to avoid leaking droplets if count changed between provision and destroy.
    if [ ! -d "$LOCAL_DIR/crn" ]; then
        echo "==> No CRN state found."
        return
    fi
    for dir in "$LOCAL_DIR/crn"/*/; do
        [ -d "$dir" ] || continue
        if [ -f "$dir/droplet-name" ]; then
            local name
            name=$(cat "$dir/droplet-name")
            echo "==> Deleting droplet $name ..."
            doctl compute droplet delete --force "$name" 2>/dev/null || true
        fi
        rm -rf "$dir"
    done
    echo "==> CRN droplets destroyed."
}

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
case "${1:-}" in
    --provision)
        provision
        ;;
    --install)
        install_crn
        ;;
    --register)
        register_crn
        ;;
    --status)
        status_crn
        ;;
    --destroy)
        destroy
        ;;
    "")
        provision
        install_crn
        register_crn
        ;;
    --help|-h)
        echo "Usage: $0 [--provision|--install|--register|--status|--destroy]"
        exit 0
        ;;
    *)
        echo "Usage: $0 [--provision|--install|--register|--status|--destroy]"
        exit 1
        ;;
esac
