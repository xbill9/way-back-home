# Makefile for Biometric Security System

.PHONY: clean test lint deploy run endpoint frontend mock adk

clean:
	@echo "Cleaning up logs, caches, and temporary files..."
	find . -type f -name "*.log" -delete 2>/dev/null || true
	find . -type f -name "*.pyc" -delete 2>/dev/null || true
	find . -type d -name "__pycache__" -exec rm -rf {} + 2>/dev/null || true
	find . -type d -name ".adk" -exec rm -rf {} + 2>/dev/null || true
	find . -type d -name ".pytest_cache" -exec rm -rf {} + 2>/dev/null || true
	find . -type d -name ".ruff_cache" -exec rm -rf {} + 2>/dev/null || true
	rm -rf frontend/dist 2>/dev/null || true
	@echo "Clean completed."

test:
	python -m pytest

lint:
	ruff check .
	ruff format --check .
	cd frontend && npm run lint

frontend:
	cd frontend && npm install && npm run build

endpoint:
	@gcloud run services describe ${SERVICE_NAME} --platform=managed --region=${REGION} --format='value(status.url)'

deploy:
	gcloud run deploy biometric-scout \
  --image=${IMAGE_PATH} \
  --platform=managed \
  --region=${GOOGLE_CLOUD_LOCATION} \
  --allow-unauthenticated \
  --labels=dev-tutorial=multi-modal \
  --set-env-vars="GOOGLE_CLOUD_PROJECT=${PROJECT_ID}" \
  --set-env-vars="GOOGLE_CLOUD_LOCATION=${GOOGLE_CLOUD_LOCATION}" \
  --set-env-vars="GOOGLE_GENAI_USE_VERTEXAI=False" \
  --set-env-vars="GOOGLE_API_KEY=${GOOGLE_API_KEY}" \
  --set-env-vars="GEMINI_API_KEY=${GOOGLE_API_KEY}" \
  --set-env-vars="GEMINI_KEY=${GOOGLE_API_KEY}" \
  --set-env-vars="MODEL_ID=gemini-3.1-flash-live-preview"

run:
	. ./biosync.sh

mock:
	. ./mock.sh

adk:
	. ./runadk.sh

testadk:
	. ./testadk.sh
