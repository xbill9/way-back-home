#!/bin/bash
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")"

if [ ! -f .env ]; then
    echo "Error: .env not found. Run ./init.sh or ./set_env.sh first." >&2
    exit 1
fi

# 'set -a' marks every assignment in .env for export so list_models.py, which
# runs as a child process, actually inherits them.
set -a
# shellcheck disable=SC1091
source .env
set +a

python3 list_models.py
