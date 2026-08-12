#!/bin/bash
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")"

# Check if gcloud is authenticated
if ! gcloud auth list --filter=status:ACTIVE --format="value(account)" | grep -q "@"; then
    echo "Error: No active gcloud account found." >&2
    echo "Please run 'gcloud auth login' and try again." >&2
    exit 1
fi

# Get current project
PROJECT_ID=$(gcloud config get-value project 2>/dev/null)
if [ "$PROJECT_ID" == "(unset)" ] || [ -z "$PROJECT_ID" ]; then
    echo "Error: No gcloud project is currently set." >&2
    echo "Run 'gcloud config set project [PROJECT_ID]' to configure it." >&2
    exit 1
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

echo "Current Environment"
sed -E 's/^((GOOGLE_API_KEY|GEMINI_API_KEY|GEMINI_KEY)=).*/\1<redacted>/' .env

echo "Cloud Login"
gcloud auth list

echo "ADK Version"
adk --version
