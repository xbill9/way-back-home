# Alpha Rescue Drone - Biometric Security System

This project implements a real-time biometric security system for the Alpha Rescue Drone Fleet. It uses Gemini's multimodal capabilities to verify hand gestures via a live video stream.

## Project Structure

-   `backend/`: FastAPI server using Google ADK (Agent Development Kit).
    -   `app/main.py`: Main entry point for the FastAPI server and WebSocket handler.
    -   `app/biometric_agent/agent.py`: Definition of the Biometric Agent and its tools.
-   `frontend/`: React application built with Vite and Tailwind CSS.
    -   `src/BiometricLock.jsx`: Core UI component for the biometric scanning interface.
    -   `src/useGeminiSocket.js`: Custom hook for managing the WebSocket connection to the backend.
-   `mock/`: Mock data and server for testing.
-   `scripts/`: Utility scripts for setup and verification.

## Features

-   **Real-time Hand Gesture Recognition**: Detects the number of fingers shown (1-5) using Gemini 3.1 Flash Live.
-   **Biometric Handshake**: Executes a server-side tool (`report_digit`) upon successful detection.
-   **Secure Authentication Sequence**: Requires a sequence of finger counts to "unlock" the system.
-   **Multimodal Interaction**: Supports both video and audio input for a seamless user experience.

## Getting Started

### Prerequisites

-   Google Cloud Project.
-   Gemini API Key.
-   Node.js and npm.
-   Python 3.10+.

### Setup

1.  Initialize the environment:
    ```bash
    ./init.sh
    ```
    This script will prompt for your Project ID and Gemini API Key, and set up the `.env` file and Python dependencies.

2.  Install frontend dependencies:
    ```bash
    ./frontend.sh
    ```
    (Note: `frontend.sh` typically runs `npm install` and `npm run build` or starts the dev server).

### Running the Application

1.  Start the backend:
    ```bash
    ./runadk.sh
    ```
    This will start the FastAPI server on `http://localhost:8080`.

2.  Start the frontend:
    ```bash
    cd frontend
    npm run dev
    ```
    Access the application at the URL provided by Vite (usually `http://localhost:5173`).

## Development Tools

-   `Makefile`: Contains common tasks like `clean`, `test`, and `deploy`.
-   `testadk.sh`: Runs automated tests for the ADK agent.
-   `mock.sh`: Starts a mock server for local testing without calling the actual Gemini API.

## Deployment

The project is designed to be deployed to Google Cloud Run. Use the `deploy.sh` script or `make deploy` to initiate the deployment process.

```bash
make deploy SERVICE_NAME=alpha-drone REGION=us-central1 IMAGE_PATH=gcr.io/YOUR_PROJECT/alpha-drone
```
