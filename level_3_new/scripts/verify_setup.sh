#!/bin/bash
# Deliberately no 'set -e': this is a checker, so every check must run even
# after an earlier one fails.
set -uo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")/.."

# ==============================================================================
# Verify Setup Script - Mission Alpha (Level 3)
#
# Checks that the environment is correctly configured:
# 1. Google Cloud Project is set
# 2. Required APIs are enabled
# 3. Python dependencies are installed
# 4. .env configuration exists
#
# Exits 0 if every check passes, 1 otherwise, so it can gate a build.
# ==============================================================================

# --- Colors for Output ---
GREEN='\033[0;32m'
RED='\033[0;31m'
YELLOW='\033[1;33m'
BOLD='\033[1m'
NC='\033[0m' # No Color

echo -e "${BOLD}🚀 Verifying Mission Alpha (Level 3) Infrastructure...${NC}\n"

ALL_PASSED=true

# ------------------------------------------------------------------------------
# 1. Check Google Cloud Project
# ------------------------------------------------------------------------------
# Try to get project from gcloud config, suppress errors
PROJECT_ID=$(gcloud config get-value project 2>/dev/null)

# Fallback to environment variable if gcloud config returned nothing or (unset)
if [ -z "$PROJECT_ID" ] || [ "$PROJECT_ID" == "(unset)" ]; then
    PROJECT_ID=${GOOGLE_CLOUD_PROJECT:-}
fi

PROJECT_OK=false
if [ -n "$PROJECT_ID" ] && [ "$PROJECT_ID" != "(unset)" ]; then
    echo -e "✅ Google Cloud Project: ${GREEN}${PROJECT_ID}${NC}"
    PROJECT_OK=true
else
    echo -e "❌ Google Cloud Project: ${RED}Not Configured${NC}"
    echo "   Run: gcloud config set project YOUR_PROJECT_ID"
    ALL_PASSED=false
fi

# ------------------------------------------------------------------------------
# 2. Check Cloud APIs (Only if Project ID is found)
# ------------------------------------------------------------------------------
# Keep this list in sync with the 'gcloud services enable' calls in init.sh.
if [ "$PROJECT_OK" = true ]; then
    REQUIRED_APIS=(
        "aiplatform.googleapis.com"
        "run.googleapis.com"
        "cloudbuild.googleapis.com"
        "artifactregistry.googleapis.com"
        "secretmanager.googleapis.com"
    )
    MISSING_APIS=()

    # Get enabled services list once to speed up execution
    if ! ENABLED_SERVICES=$(gcloud services list --enabled --format="value(config.name)" --project="$PROJECT_ID" 2>/dev/null); then
        echo -e "⚠️  Cloud APIs: ${YELLOW}Could not verify (gcloud error or permissions issue)${NC}"
    else
        for API in "${REQUIRED_APIS[@]}"; do
            # -x -F: whole-line, fixed-string match. A substring match would let
            # an unrelated service name satisfy the check.
            if ! grep -qxF "$API" <<< "$ENABLED_SERVICES"; then
                MISSING_APIS+=("$API")
            fi
        done

        if [ ${#MISSING_APIS[@]} -eq 0 ]; then
            echo -e "✅ Cloud APIs: ${GREEN}Active${NC}"
        else
            echo -e "❌ Cloud APIs: ${RED}Missing ${MISSING_APIS[*]}${NC}"
            echo "   Run: gcloud services enable ${MISSING_APIS[*]}"
            ALL_PASSED=false
        fi
    fi
fi

# ------------------------------------------------------------------------------
# 3. Check Python Dependencies
# ------------------------------------------------------------------------------
# Format: "PipPackageName:PythonImportName"
DEPS=(
    "fastapi:fastapi"
    "uvicorn:uvicorn"
    "google-genai:google.genai"
    "websockets:websockets"
    "python-dotenv:dotenv"
    "google-adk:google.adk"
)

MISSING_DEPS=()

for DEP in "${DEPS[@]}"; do
    PKG_NAME="${DEP%%:*}"    # String before colon
    IMPORT_NAME="${DEP##*:}" # String after colon

    # Try to import the module silently
    if ! python3 -c "import $IMPORT_NAME" 2>/dev/null; then
        MISSING_DEPS+=("$PKG_NAME")
    fi
done

if [ ${#MISSING_DEPS[@]} -eq 0 ]; then
    echo -e "✅ Python Environment: ${GREEN}Ready${NC}"
else
    echo -e "❌ Python Dependencies: ${RED}Missing ${MISSING_DEPS[*]}${NC}"
    # A plain `pip install -r requirements.txt` cannot resolve: websockets==17.0.1
    # is pinned above the caps google-adk (<16) and google-genai (<17) declare.
    # install_deps.sh applies overrides.txt via uv --override or a pip two-step.
    echo "   Run: ./scripts/install_deps.sh"
    ALL_PASSED=false
fi

# Test tooling is not needed at runtime, so a miss is a warning, not a failure.
if python3 -c "import pytest" 2>/dev/null; then
    echo -e "✅ Test Tooling: ${GREEN}Ready${NC}"
else
    echo -e "⚠️  Test Tooling: ${YELLOW}pytest not installed${NC} — 'make test' will not run"
    echo "   Run: ./scripts/install_deps.sh --dev"
fi

# ------------------------------------------------------------------------------
# 4. Check .env Configuration
# ------------------------------------------------------------------------------
MISSING_ENV=()
for ENV_FILE in ".env" "backend/app/biometric_agent/.env"; do
    if [ ! -s "$ENV_FILE" ]; then
        MISSING_ENV+=("$ENV_FILE")
    elif ! grep -qE '^GOOGLE_API_KEY=.+' "$ENV_FILE"; then
        MISSING_ENV+=("$ENV_FILE (no GOOGLE_API_KEY)")
    fi
done

if [ ${#MISSING_ENV[@]} -eq 0 ]; then
    echo -e "✅ Env Configuration: ${GREEN}Present${NC}"
else
    echo -e "❌ Env Configuration: ${RED}Missing ${MISSING_ENV[*]}${NC}"
    echo "   Run: ./init.sh (root .env) and ./runadk.sh (agent .env)"
    ALL_PASSED=false
fi

# ------------------------------------------------------------------------------
# Final Summary
# ------------------------------------------------------------------------------
echo -e "\n-------------------------------------------------------"
if [ "$ALL_PASSED" = true ]; then
    echo -e "🎉 ${GREEN}${BOLD}SYSTEMS ONLINE. READY FOR MISSION.${NC}"
    exit 0
else
    echo -e "🛑 ${RED}${BOLD}SYSTEM CHECKS FAILED.${NC} Please resolve the issues above."
    exit 1
fi
