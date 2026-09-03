# Makefile for Biometric Security System

.PHONY: clean test lint deploy build run endpoint frontend mock adk testadk verify \
	record eval submission

# Overridable from the shell; kept consistent with the defaults in deploy.sh.
SERVICE_NAME ?= biometric-scout
REGION ?= us-central1

# Where session recordings for the EAP eval bundle live. Outside the repo on
# purpose: they contain microphone and camera data. See evals/README.md.
EVAL_CAPTURE_DIR ?= $(HOME)/eval-captures

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

verify:
	./scripts/verify_setup.sh

frontend:
	./frontend.sh

endpoint:
	@gcloud run services describe $(SERVICE_NAME) --platform=managed --region=$(REGION) --format='value(status.url)'

build:
	./build.sh

# Delegates to deploy.sh so there is one deployment definition. The key is
# pulled from Secret Manager there rather than passed as a plaintext env var.
deploy:
	./deploy.sh

run:
	./biosync.sh

mock:
	./mock.sh

adk:
	./runadk.sh

testadk:
	./testadk.sh

# EAP eval bundle: record -> label -> score -> package. Recording is the real
# billed backend with capture switched on; it records your microphone and camera,
# which is why nothing turns it on by default.
record:
	EVAL_CAPTURE_DIR=$(EVAL_CAPTURE_DIR) ./biosync.sh

eval:
	python evals/score_session.py --all $(EVAL_CAPTURE_DIR)

submission:
	python evals/package_submission.py --all $(EVAL_CAPTURE_DIR)
