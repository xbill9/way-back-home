export PROJECT_ID=$(cat ~/project_id.txt)
export REGION=us-central1
export SERVICE_NAME=biometric-scout
export IMAGE_PATH=gcr.io/${PROJECT_ID}/${SERVICE_NAME}


gcloud run deploy ${SERVICE_NAME} \
  --image=${IMAGE_PATH} \
  --platform=managed \
  --region=${REGION} \
  --allow-unauthenticated \
  --labels=dev-tutorial=multi-modal \
  --set-env-vars="GOOGLE_CLOUD_PROJECT=${PROJECT_ID}" \
  --set-env-vars="GOOGLE_CLOUD_LOCATION=${REGION}" \
  --set-env-vars="GOOGLE_GENAI_USE_VERTEXAI=False" \
  --set-env-vars="GOOGLE_API_KEY=$GOOGLE_API_KEY" \
  --set-env-vars="GEMINI_API_KEY=$GOOGLE_API_KEY" \
  --set-env-vars="GEMINI_KEY=$GOOGLE_API_KEY" \
  --set-env-vars="MODEL_ID=gemini-3.1-flash-live-preview"
