#!/bin/bash
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")/frontend"

# `npm ci`, not `npm install`: build the exact locked tree, the same way the
# Dockerfile's builder stage does. Run `npm install` by hand when you actually
# intend to bump package-lock.json.
npm ci
npm run build
