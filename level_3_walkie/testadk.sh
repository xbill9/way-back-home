#!/bin/bash
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")/backend/app"

echo 'connect to local ADK CLI'
echo
adk run biometric_agent
