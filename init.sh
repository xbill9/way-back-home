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

# Add $HOME/.local/bin to PATH if running in Cloud Shell
if [ -n "$CLOUD_SHELL" ]; then
    export PATH="$HOME/.local/bin:$PATH"
fi

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
else
    read -p "Enter Project ID: " PROJECT_ID
    echo "$PROJECT_ID" > "$HOME/project_id.txt"	
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

export SERVICE_NAME=biometric-scout
export IMAGE_PATH=gcr.io/${PROJECT_ID}/${SERVICE_NAME}

cd $HOME/way-back-home/level_3/backend/
cp app/biometric_agent/.env app/.env

cd $HOME/way-back-home/level_3/scripts
chmod +x verify_setup.sh
source verify_setup.sh

echo "\n--- Full Setup Complete ---"

