#!/bin/bash

# --- Function for error handling ---
handle_error() {
  echo -e "\n\n*******************************************************"
  echo "Error: $1"
  echo "*******************************************************"
  # Instead of exiting, we warn the user and wait for input
  echo "The script encountered an error."
  echo "Press [Enter] to ignore this error and attempt to continue."
  echo "Press [Ctrl+C] to exit the script completely."
  read -r # Pauses script here
}

# Check if gcloud is authenticated
if ! gcloud auth list --filter=status:ACTIVE --format="value(account)" | grep -q "@"; then
    echo "Error: No active gcloud account found."
    echo "Please run 'gcloud auth login' and try again."
    exit 1
fi

if [ -z "$CLOUD_SHELL" ]; then
    if ! gcloud auth application-default print-access-token > /dev/null 2>&1; then
        echo "ADC expired or not found. Initializing login..."
        gcloud auth application-default login
    else
        echo "ADC is valid."
    fi
fi


# --- Part 1: Find or Create Google Cloud Project ID ---
PROJECT_FILE="$HOME/project_id.txt"
PROJECT_ID_SET=false

# Check if a project ID file already exists and points to a valid project
if [ -s "$PROJECT_FILE" ]; then
    EXISTING_PROJECT_ID=$(cat "$PROJECT_FILE" | tr -d '[:space:]') # Read and trim whitespace
    echo "--- Found existing project ID in $PROJECT_FILE: $EXISTING_PROJECT_ID ---"
    echo "Verifying this project exists in Google Cloud..."

    # Check if the project actually exists in GCP and we have permission to see it
    if gcloud projects describe "$EXISTING_PROJECT_ID" --quiet >/dev/null 2>&1; then
        echo "Project '$EXISTING_PROJECT_ID' successfully verified."
        FINAL_PROJECT_ID=$EXISTING_PROJECT_ID
        PROJECT_ID_SET=true

        # Ensure gcloud config is set to this project for the current session
        gcloud config set project "$FINAL_PROJECT_ID" || handle_error "Failed to set active project to '$FINAL_PROJECT_ID'."
        echo "Set active gcloud project to '$FINAL_PROJECT_ID'."
    else
        echo "Warning: Project '$EXISTING_PROJECT_ID' from file does not exist or you lack permissions."
        echo "Removing invalid reference file and proceeding with new project creation."
        rm "$PROJECT_FILE"
    fi
fi

# If no valid project_id.txt, check currently active gcloud project
if [ "$PROJECT_ID_SET" = false ]; then
    ACTIVE_PROJECT_ID=$(gcloud config get-value project 2>/dev/null)
    if [ "$ACTIVE_PROJECT_ID" = "(unset)" ]; then ACTIVE_PROJECT_ID=""; fi

    if [ -n "$ACTIVE_PROJECT_ID" ]; then
        echo "Detected currently active project: $ACTIVE_PROJECT_ID"
        if gcloud projects describe "$ACTIVE_PROJECT_ID" --quiet >/dev/null 2>&1; then
            echo "Verified '$ACTIVE_PROJECT_ID' access. Using it."
            FINAL_PROJECT_ID="$ACTIVE_PROJECT_ID"
            PROJECT_ID_SET=true
        else
            echo "Warning: Active project '$ACTIVE_PROJECT_ID' is not accessible."
        fi
    fi
fi

# Search for an existing waybackhome-* project
if [ "$PROJECT_ID_SET" = false ]; then
    echo "Searching for existing waybackhome project..."
    FOUND_PROJECT=$(gcloud projects list --filter="projectId:waybackhome-*" --format="value(projectId)" --sort-by=~createTime --limit=1 2>/dev/null)

    if [ -n "$FOUND_PROJECT" ]; then
        echo "✓ Found existing project: $FOUND_PROJECT"
        FINAL_PROJECT_ID="$FOUND_PROJECT"
        PROJECT_ID_SET=true
        gcloud config set project "$FINAL_PROJECT_ID" --quiet 2>/dev/null
        echo "$FINAL_PROJECT_ID" > "$PROJECT_FILE"
    fi
fi

# Last resort: offer to create a new project or enter an existing one
if [ "$PROJECT_ID_SET" = false ]; then
    echo "--- No existing waybackhome project found. Let's set one up. ---"
    CODELAB_PROJECT_PREFIX="waybackhome"

    # Dynamic Length Calculation
    PREFIX_LEN=${#CODELAB_PROJECT_PREFIX}
    if (( PREFIX_LEN > 25 )); then
      handle_error "The project prefix '$CODELAB_PROJECT_PREFIX' is too long (${PREFIX_LEN} chars). Maximum is 25."
    fi
    MAX_SUFFIX_LEN=$(( 30 - PREFIX_LEN - 1 ))

    # Loop until a project is successfully created or selected
    while true; do
      RANDOM_SUFFIX=$(LC_ALL=C tr -dc 'a-z0-9' < /dev/urandom | head -c "$MAX_SUFFIX_LEN")
      SUGGESTED_PROJECT_ID="${CODELAB_PROJECT_PREFIX}-${RANDOM_SUFFIX}"

      echo ""
      echo "Select a Project ID:"
      echo "  1. Press Enter to CREATE a new project: $SUGGESTED_PROJECT_ID"
      echo "  2. Or type an existing Project ID to use."
      read -p "Project ID: " USER_INPUT

      FINAL_PROJECT_ID="${USER_INPUT:-$SUGGESTED_PROJECT_ID}"

      if [[ -z "$FINAL_PROJECT_ID" ]]; then
          echo "Project ID cannot be empty. Please try again."
          continue
      fi

      echo "Checking status of '$FINAL_PROJECT_ID'..."

      # Check if exists/accessible
      if gcloud projects describe "$FINAL_PROJECT_ID" >/dev/null 2>&1; then
        echo "Project '$FINAL_PROJECT_ID' exists and is accessible."
        gcloud config set project "$FINAL_PROJECT_ID" || handle_error "Failed to set active project to $FINAL_PROJECT_ID."
        echo "$FINAL_PROJECT_ID" > "$PROJECT_FILE" || handle_error "Failed to save project ID to $PROJECT_FILE."
        echo "Successfully saved project ID to $PROJECT_FILE."
        break
      else
        # Try to create it
        echo "Project '$FINAL_PROJECT_ID' not found. Attempting to create..."
        if gcloud projects create "$FINAL_PROJECT_ID" --quiet; then
          echo "Successfully created project: $FINAL_PROJECT_ID"
          gcloud config set project "$FINAL_PROJECT_ID" || handle_error "Failed to set active project to $FINAL_PROJECT_ID."
          echo "$FINAL_PROJECT_ID" > "$PROJECT_FILE" || handle_error "Failed to save project ID to $PROJECT_FILE."
          echo "Successfully saved project ID to $PROJECT_FILE."
          break
        else
          echo "Failed to create '$FINAL_PROJECT_ID'. Please try a different ID."
        fi
      fi
    done
fi

# --- Part 2: Install Dependencies and Run Billing Setup ---
echo "\n--- Installing Python dependencies ---"
# Using || handle_error means if it fails, it will pause, allow you to read, and then proceed
pip install --upgrade --user google-cloud-billing || handle_error "Failed to install Python libraries."

echo "\n--- Running the Billing Enablement Script ---"
#python3 billing-enablement.py || handle_error "The billing enablement script failed."

echo "\n--- set Project id ---"
gcloud config set project $(cat ~/project_id.txt)

echo "\n--- Enable APIs ---"
gcloud services enable  compute.googleapis.com \
                        artifactregistry.googleapis.com \
                        run.googleapis.com \
                        cloudbuild.googleapis.com \
                        iam.googleapis.com \
                        aiplatform.googleapis.com

cd $HOME/way-back-home/level_3
pip install -r requirements.txt


cd $HOME/way-back-home/level_3/backend/app/biometric_agent
echo "GOOGLE_CLOUD_PROJECT=$(cat ~/project_id.txt)" > .env
echo "GOOGLE_CLOUD_LOCATION=us-central1" >> .env
echo "GOOGLE_GENAI_USE_VERTEXAI=True" >> .env

cd $HOME/way-back-home/level_3/backend/
cp app/biometric_agent/.env app/.env

cd $HOME/way-back-home/level_3/scripts
chmod +x verify_setup.sh
source verify_setup.sh

echo "\n--- Full Setup Complete ---"

