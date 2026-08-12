#!/bin/bash
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")/backend"

echo "Local URL"
echo "http://127.0.0.1:8080/"

python app/main.py
