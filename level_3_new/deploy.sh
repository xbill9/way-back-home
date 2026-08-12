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
REGION="${REGION:-us-central1}"
SERVICE_NAME="${SERVICE_NAME:-biometric-scout}"
IMAGE_PATH="${IMAGE_PATH:-gcr.io/${PROJECT_ID}/${SERVICE_NAME}}"
SECRET_NAME="${SECRET_NAME:-gemini-api-key}"

# ---------------------------------------------------------------------------
# Sync the API key into Secret Manager.
#
# The key is never passed on the gcloud command line and never stored in the
# Cloud Run revision spec, so it does not leak to anyone holding
# run.services.get or to retained build history.
# ---------------------------------------------------------------------------
KEY_VALUE=$(<"$KEY_FILE")

# gcloud --data-file uploads the file byte-for-byte, so write a copy without
# the trailing newline; otherwise the comparison below never matches and every
# deploy adds a redundant secret version. mktemp creates it mode 600.
TMP_KEY=$(mktemp)
trap 'rm -f "$TMP_KEY"' EXIT
printf '%s' "$KEY_VALUE" > "$TMP_KEY"

if ! gcloud secrets describe "$SECRET_NAME" --project="$PROJECT_ID" >/dev/null 2>&1; then
    echo "Creating secret ${SECRET_NAME}..."
    gcloud secrets create "$SECRET_NAME" \
      --replication-policy=automatic \
      --project="$PROJECT_ID"
fi

CURRENT_VALUE=$(gcloud secrets versions access latest \
  --secret="$SECRET_NAME" --project="$PROJECT_ID" 2>/dev/null || true)

if [ "$CURRENT_VALUE" != "$KEY_VALUE" ]; then
    echo "Adding new version of ${SECRET_NAME}..."
    gcloud secrets versions add "$SECRET_NAME" \
      --data-file="$TMP_KEY" \
      --project="$PROJECT_ID"
fi

# Let the Cloud Run runtime service account read the secret (idempotent).
PROJECT_NUMBER=$(gcloud projects describe "$PROJECT_ID" --format='value(projectNumber)')
RUNTIME_SA="${PROJECT_NUMBER}-compute@developer.gserviceaccount.com"

gcloud secrets add-iam-policy-binding "$SECRET_NAME" \
  --member="serviceAccount:${RUNTIME_SA}" \
  --role=roles/secretmanager.secretAccessor \
  --project="$PROJECT_ID" >/dev/null

# ---------------------------------------------------------------------------
# Deploy.
#
# --set-env-vars replaces the whole environment, so every variable must go in
# a single comma-separated flag. Repeating the flag keeps only the last one.
# ---------------------------------------------------------------------------
gcloud run deploy "${SERVICE_NAME}" \
  --image="${IMAGE_PATH}" \
  --platform=managed \
  --region="${REGION}" \
  --project="${PROJECT_ID}" \
  --allow-unauthenticated \
  --labels=dev-tutorial=multi-modal \
  --set-env-vars="GOOGLE_CLOUD_PROJECT=${PROJECT_ID},GOOGLE_CLOUD_LOCATION=${REGION},GOOGLE_GENAI_USE_VERTEXAI=False,MODEL_ID=gemini-3.1-flash-live-preview" \
  --set-secrets="GOOGLE_API_KEY=${SECRET_NAME}:latest,GEMINI_API_KEY=${SECRET_NAME}:latest,GEMINI_KEY=${SECRET_NAME}:latest"
