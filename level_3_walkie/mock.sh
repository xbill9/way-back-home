#!/bin/bash
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")"

echo "http://127.0.0.1:8080/"

python mock/mock_server.py
