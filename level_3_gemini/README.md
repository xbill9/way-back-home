# Alpha Rescue Drone - Biometric Security System (Mission Alpha)

This project implements a real-time biometric security system for the Alpha Rescue Drone Fleet. It leverages **Gemini 3.1 Flash Live** and the **Google Agent Development Kit (ADK)** to verify hand gestures via a live multimodal stream.

## Overview

The system acts as a "Security Interrogator" that requires a specific sequence of hand gestures (finger counts 1-5) to unlock the drone's neural link. It uses high-speed video analysis (2Hz) and low-latency audio feedback to create a seamless, futuristic security handshake.

## Features

-   **Real-time Hand Gesture Recognition**: Detects the number of fingers shown (1-5) with sub-second latency.
-   **4-Digit Biometric Handshake**: Users must successfully show a randomized sequence of 4 digits within 65 seconds to authenticate.
-   **Offensive Gesture Detection**: Includes a critical safety protocol that triggers a `system_error` if the middle finger is detected.
-   **Heavy Metal Override (Protocol: Sabbath)**: A secret authentication bypass that activates `heavy_metal_mode` when the "Devil's Horns" gesture is detected (index and pinky extended).
    -   *Bonus*: The frontend plays the "War Pigs" intro to celebrate successful override.
-   **Neural Link Startup Sequence**: A visual and audio handshake ensures synchronization. Users hear "Biometric Scanner Online" before the sequence begins.
-   **Robotic Persona**: The agent maintains a cold, monotone, and efficient persona, providing minimal but precise verbal confirmations (e.g., "Two digits.").
-   **Mock Server Integration**: A high-fidelity mock mode (`mock.sh`) allows for frontend development with a persistent banner ticker and simulated model responses.

-   **Multimodal Streaming**: Bidirectional WebSocket connection handling synchronized Video (frames), Audio (16kHz PCM), and Text.
-   **Gemini 3.1 Flash Live Integration**: Optimized for the latest Live API capabilities, including native audio processing and complex tool-calling.

## Project Structure

-   `backend/`: FastAPI server using Google ADK.
    -   `app/main.py`: WebSocket handler, session management, and keep-alive heartbeats.
    -   `app/biometric_agent/agent.py`: Agent definition, instructions, and tools (`report_digit`, `trigger_system_error`, `trigger_heavy_metal_mode`).
    -   `app/patch_adk.py`: Compatibility patches for Gemini 3.1 Live API.
-   `frontend/`: React application built with Vite and Tailwind CSS.
    -   `src/BiometricLock.jsx`: Core UI with "Neon Cyan" aesthetic and real-time feedback.
    -   `src/useGeminiSocket.js`: Custom hook managing the multimodal WebSocket stream.
-   `mock/`: Mock audio and server for local development without API credits.

## Getting Started

### Prerequisites

-   Google Cloud Project with Vertex AI API enabled.
-   Gemini API Key (or Google Cloud credentials for Vertex AI).
-   Node.js (v18+) and Python (3.10+).

### Setup

1.  **Initialize Environment**:
    ```bash
    ./init.sh
    ```
    Follow the prompts to configure your Project ID and API Key.

2.  **Verify Infrastructure**:
    ```bash
    ./scripts/verify_setup.sh
    ```

3.  **Install Frontend**:
    ```bash
    ./frontend.sh
    ```

### Running the Application

1.  **Start Backend**:
    ```bash
    ./runadk.sh
    ```
    The server starts on `http://localhost:8080`.

2.  **Start Frontend**:
    ```bash
    cd frontend
    npm run dev
    ```
    Access the UI at `http://localhost:5173`.

## Development & Testing

-   **Mock Mode**: Run `./mock.sh` to start a mock backend that simulates Gemini's responses.
-   **Agent Tests**: Run `./testadk.sh` to execute automated tests against the `biometric_agent`.
-   **Model Testing**: Use `testmodels.sh` to verify model availability and connectivity.

## Deployment

Deploy to Google Cloud Run using the provided `Makefile`:

```bash
make deploy
```
Ensure your environment variables are correctly configured in the Cloud Run service settings.
