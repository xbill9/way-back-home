#!/bin/bash
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")"

# Check if gcloud is authenticated
if ! gcloud auth list --filter=status:ACTIVE --format="value(account)" | grep -q "@"; then
    echo "Error: No active gcloud account found." >&2
    echo "Please run 'gcloud auth login' and try again." >&2
    exit 1
fi

if [ -s "$HOME/project_id.txt" ]; then
    PROJECT_ID=$(<"$HOME/project_id.txt")
else
    read -r -p "Enter Project ID: " PROJECT_ID
    printf '%s\n' "$PROJECT_ID" > "$HOME/project_id.txt"
fi

if [ -s "$HOME/gemini.key" ]; then
    GOOGLE_API_KEY=$(<"$HOME/gemini.key")
else
    # -s keeps the key off the screen and out of the scrollback.
    read -r -s -p "Enter Gemini KEY: " GOOGLE_API_KEY
    echo
    printf '%s\n' "$GOOGLE_API_KEY" > "$HOME/gemini.key"
    chmod 600 "$HOME/gemini.key"
fi

gcloud config set project "$PROJECT_ID"

# enable services

gcloud services enable aiplatform.googleapis.com
gcloud services enable cloudresourcemanager.googleapis.com
gcloud services enable artifactregistry.googleapis.com
gcloud services enable cloudbuild.googleapis.com
gcloud services enable run.googleapis.com
gcloud services enable cloudaicompanion.googleapis.com
gcloud services enable secretmanager.googleapis.com

# Restrict permissions before any secret material is written.
: > .env
chmod 600 .env

cat <<EOF > .env
GOOGLE_GENAI_USE_VERTEXAI=false
GOOGLE_CLOUD_PROJECT=$PROJECT_ID
GOOGLE_CLOUD_LOCATION=us-central1
IMAGEN_MODEL="imagen-4.0-fast-generate-001"
GENAI_MODEL="gemini-2.5-flash"
GOOGLE_API_KEY=$GOOGLE_API_KEY
GEMINI_API_KEY=$GOOGLE_API_KEY
GEMINI_KEY=$GOOGLE_API_KEY
MODEL_ID="gemini-3.1-flash-live-preview"
SERVICE_NAME=biometric-scout
EOF

set -a
# shellcheck disable=SC1091
source .env
set +a

if [ -z "${CLOUD_SHELL:-}" ]; then
    if ! gcloud auth application-default print-access-token > /dev/null 2>&1; then
        echo "ADC expired or not found. Initializing login..."
        gcloud auth application-default login
    else
        echo "ADC is valid."
    fi
fi

# Reinstall whenever requirements.txt or the override changes, not just on the
# first run. install_deps.sh applies the websockets override; a bare
# `pip install -r requirements.txt` cannot resolve and would abort this script
# under `set -e`. See overrides.txt.
REQ_STAMP=".requirements_installed"
REQ_HASH=$(sha256sum requirements.txt overrides.txt | sha256sum | cut -d' ' -f1)
if [ ! -f "$REQ_STAMP" ] || [ "$(<"$REQ_STAMP")" != "$REQ_HASH" ]; then
    ./scripts/install_deps.sh
    printf '%s\n' "$REQ_HASH" > "$REQ_STAMP"
fi

echo "Environment setup"
sed -E 's/^((GOOGLE_API_KEY|GEMINI_API_KEY|GEMINI_KEY)=).*/\1<redacted>/' .env

echo "Cloud Login"
gcloud auth list

# Deliberately NOT `pip install google-adk --upgrade`: requirements.txt pins
# google-adk==2.6.3, and an unpinned upgrade re-resolves its websockets>=15,<16
# cap, silently reverting the override installed above.
echo "ADK version"
adk --version
