#!/bin/bash
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")"

PROJECT_ID_FILE="$HOME/project_id.txt"
if [ ! -s "$PROJECT_ID_FILE" ]; then
    echo "Error: $PROJECT_ID_FILE is missing or empty. Run ./init.sh first." >&2
    exit 1
fi

PROJECT_ID=$(<"$PROJECT_ID_FILE")
SERVICE_NAME="${SERVICE_NAME:-biometric-scout}"
IMAGE_PATH="${IMAGE_PATH:-gcr.io/${PROJECT_ID}/${SERVICE_NAME}}"

# Dockerfile is checked into the repo and is the source of truth. This script
# used to regenerate it from a heredoc on every run, silently discarding any
# hand edits.
gcloud builds submit . \
  --tag "${IMAGE_PATH}" \
  --project "${PROJECT_ID}"
