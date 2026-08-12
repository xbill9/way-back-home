#!/bin/bash
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")/frontend"

npm install
npm run build
