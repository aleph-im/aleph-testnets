set dotenv-load := false

venv_dir := ".venv"
venv_bin := venv_dir / "bin"
compose := "docker compose -f deploy/docker-compose.yml -f deploy/docker-compose.local.yml"

# List available recipes
default:
    @just --list

# Initialize git submodules
[private]
setup-submodules:
    git submodule update --init --recursive

# Create venv and install Python dependencies
[private]
setup-venv:
    #!/usr/bin/env bash
    set -euo pipefail
    if [ ! -d "{{ venv_dir }}" ]; then
        echo "==> Creating virtualenv..."
        python3 -m venv "{{ venv_dir }}"
    fi
    "{{ venv_bin }}/pip" install -q .

# Generate .env from manifesto and download CLI
[private]
setup-env: setup-venv
    #!/usr/bin/env bash
    set -euo pipefail
    export PATH="{{ venv_bin }}:$PATH"
    ./scripts/local-up.sh --env

# Start the full dev environment (containers + contracts + indexer)
start-dev-env: setup-submodules setup-env
    #!/usr/bin/env bash
    set -euo pipefail
    ./scripts/local-up.sh --up
    ./scripts/local-up.sh --deploy-contracts

# Run integration tests (extra args passed through, e.g. just test -k test_credit)
test *args='': setup-env
    #!/usr/bin/env bash
    set -euo pipefail
    export PATH="{{ venv_bin }}:$PATH"
    ./scripts/local-up.sh --test {{ args }}

# Stop all containers (state is preserved for quick restart)
stop-dev-env:
    {{ compose }} --profile credits stop

# Stop containers and wipe all state (volumes, keys, configs)
reset-dev-env:
    #!/usr/bin/env bash
    set -euo pipefail
    ./scripts/local-up.sh --down
    rm -rf "{{ venv_dir }}"
    echo "==> Dev environment fully reset."

# Show container logs
logs:
    ./scripts/local-up.sh --logs

# Remove all generated files (venv, build artifacts, caches)
clean:
    #!/usr/bin/env bash
    set -euo pipefail
    rm -rf "{{ venv_dir }}"
    rm -rf .local bin results.xml
    rm -f deploy/.env deploy/config.yml
    rm -rf deploy/indexer
    rm -rf contracts/broadcast contracts/out contracts/cache
    rm -rf __pycache__ tests/__pycache__ *.egg-info
    echo "==> Clean."
