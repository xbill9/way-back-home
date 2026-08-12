#!/bin/bash
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")"

PROJECT_ID_FILE="$HOME/project_id.txt"
KEY_FILE="$HOME/gemini.key"

for f in "$PROJECT_ID_FILE" "$KEY_FILE"; do
    if [ ! -s "$f" ]; then
        echo "Error: $f is missing or empty. Run ./init.sh first." >&2
        exit 1
    fi
done

PROJECT_ID=$(<"$PROJECT_ID_FILE")
GOOGLE_API_KEY=$(<"$KEY_FILE")

AGENT_ENV="backend/app/biometric_agent/.env"

# Restrict permissions before any secret material is written.
: > "$AGENT_ENV"
chmod 600 "$AGENT_ENV"

cat <<EOF > "$AGENT_ENV"
GOOGLE_CLOUD_PROJECT=$PROJECT_ID
GOOGLE_CLOUD_LOCATION=us-central1
GOOGLE_GENAI_USE_VERTEXAI=False
GOOGLE_API_KEY=$GOOGLE_API_KEY
GEMINI_API_KEY=$GOOGLE_API_KEY
GEMINI_KEY=$GOOGLE_API_KEY
MODEL_ID=gemini-3.1-flash-live-preview
EOF

# The dev UI loads a live API key, so it binds to loopback by default.
# Set ADK_HOST=0.0.0.0 (and widen ADK_ALLOW_ORIGINS) only on a trusted network.
ADK_HOST="${ADK_HOST:-127.0.0.1}"
ADK_ALLOW_ORIGINS="${ADK_ALLOW_ORIGINS:-http://127.0.0.1:8000,http://localhost:8000}"

cd backend/app

echo "connect on http://${ADK_HOST}:8000/"
echo
adk web --host "$ADK_HOST" --allow_origins "$ADK_ALLOW_ORIGINS"
