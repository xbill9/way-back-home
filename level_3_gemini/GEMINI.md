# ADK & Gemini 3.1 Flash Live Developer Guide

Do not ever use any 2.0 models they are depreciated.
Suggest only 2.5 models or later

This document provides technical guidance for developers working with the Google Agent Development Kit (ADK) and the Gemini 3.1 Flash Live model within this project.

## Gemini 3.1 Flash Live Overview

Gemini 3.1 Flash Live is a low-latency, natively multimodal model optimized for real-time interactions. It is a specialized variant of the Gemini 3 Pro model family.

https://ai.google.dev/gemini-api/docs/live-api/capabilities

github has open issues:

https://github.com/livekit/agents-js/pull/1186
https://github.com/google/adk-python/issues/5075
https://github.com/google/adk-python/issues/5018

use this for skills:
https://github.com/google-gemini/gemini-skills/blob/main/skills/gemini-live-api-dev/SKILL.md

live model article
https://blog.google/innovation-and-ai/models-and-research/gemini-models/gemini-3-1-flash-live/

audio implementation

https://developer.chrome.com/blog/audio-worklet

### Key Technical Specifications

-   **Model ID:** `gemini-3.1-flash-live-preview` (default in this project)
-   **Context Window:** 128K tokens (Input) / 64K tokens (Output).
-   **Modality:** Natively multimodal. Supports Text, Image, Audio, and Video as input; Text and Audio as output.
-   **Native Audio:** Directly processes and generates audio, preserving emotional nuance and pacing without external TTS/STT engines.
-   **Real-time Streaming:** Optimized for continuous data streams (video/audio) with "immediate" response latency.

### Use Case in This Project

The Biometric Security System leverages Gemini 3.1 Flash Live's **video streaming** and **complex function calling** capabilities to:
1.  Analyze a live video feed of hand gestures.
2.  Maintain a robotic, low-latency conversational persona.
3.  Execute the `report_digit` tool immediately upon visual verification of a gesture.

## Working with ADK (Agent Development Kit)

The backend uses the Google ADK to orchestrate the agent's behavior and tools.

### Agent Definition (`backend/app/biometric_agent/agent.py`)

Agents are defined using the `Agent` class, which encapsulates the model, instructions, and tools.

```python
from google.adk.agents import Agent

root_agent = Agent(
    name="biometric_agent",
    model=MODEL_ID,
    tools=[report_digit],
    instruction="..."
)
```

### Tool Implementation

Tools are standard Python functions with clear docstrings. Gemini uses these docstrings to understand when and how to call the tool.

-   **Critical Requirement:** Tool results should be handled as specified in the agent's instructions (e.g., "When you get the result of `report_digit`, DO NOT SPEAK").

### Runner and Session Service (`backend/app/main.py`)

The `Runner` connects the agent to the FastAPI application and manages the execution loop. The `InMemorySessionService` tracks state across multiple turns in a session.

```python
runner = Runner(app_name=APP_NAME, agent=root_agent, session_service=session_service)
```

### WebSocket Integration

ADK provides a bidirectional streaming interface over WebSockets. The project uses `StreamingMode.BIDI`.

-   **Native Audio Config:** For `gemini-3.1-flash-live-preview`, `response_modalities` should be set to `["AUDIO"]` to enable the native audio features and proactivity.
-   **Proactivity:** `ProactivityConfig(proactive_audio=True)` allows the model to initiate speech or tool calls without waiting for a user prompt, essential for a "live" scanner.

## Developer Workflow

1.  **Instruction Tuning:** Modify the `instruction` string in `agent.py` to refine the scanner's behavior and personality.
2.  **Tool Expansion:** Add new functions to the `tools` list in `agent.py` to expand the system's capabilities.
3.  **Local Testing:** Use `mock.sh` to test the frontend and backend orchestration without consuming Gemini API credits for every run.
4.  **Deployment:** Ensure all environment variables (especially `MODEL_ID` and `GOOGLE_API_KEY`) are correctly set in the Cloud Run configuration.

## Migrating from Gemini 2.5 Flash Live

Gemini 3.1 Flash Live Preview is optimized for low-latency, real-time dialogue. When migrating from gemini-2.5-flash-native-audio-preview-12-2025, consider the following:

- **Model string:** Update your model string from `gemini-2.5-flash-native-audio-preview-12-2025` to `gemini-3.1-flash-live-preview`.
- **Thinking configuration:** Gemini 3.1 uses `thinkingLevel` (with settings like minimal, low, medium, and high) instead of `thinkingBudget`. The default is minimal to optimize for lowest latency. See Thinking levels and budgets.
- **Server events:** A single `BidiGenerateContentServerContent` event can now contain multiple content parts simultaneously (for example, audio chunks and transcript). Update your code to process all parts in each event to avoid missing content.
- **Client content:** `send_client_content` is only supported for seeding initial context history (requires setting `initial_history_in_client_content` in `history_config`). Use `send_realtime_input` to send text updates during the conversation. See Incremental content updates.
- **Turn coverage:** Defaults to `TURN_INCLUDES_AUDIO_ACTIVITY_AND_ALL_VIDEO` instead of `TURN_INCLUDES_ONLY_ACTIVITY`. The model's turn now includes detected audio activity and all video frames. If your application currently sends a constant stream of video frames, you may want to update your application to only send video frames when there is audio activity to avoid incurring additional costs.
- **Async function calling:** Not yet supported. Function calling is synchronous only. The model will not start responding until you've sent the tool response. See Async function calling.
- **Proactive audio and affective dialogue:** These features are not yet supported in Gemini 3.1 Flash Live. Remove any configuration for these features from your code. See Proactive audio and Affective dialogue.

For a detailed feature comparison, see the Model comparison table in the capabilities guide.

## Resources

-   [Gemini 3.1 Flash Live Model Card](https://deepmind.google/models/model-cards/gemini-3-1-flash-live/)
-   [Google ADK Documentation](https://github.com/google/adk) (Internal/Preview)


The Gemini Live API enables real-time voice and video interactions with low latency. It supports bidirectional streaming of raw PCM audio via WebSockets. This API uses Native Audio for natural conversation, allowing interruptions, emotion detection, and tool use, making it suitable for voice agents. 
Google Cloud Documentation
Google Cloud Documentation
 +4
Key Components for Live Audio Implementation
AudioWorklet: Use Web Audio API's AudioWorklet to handle microphone input and audio playback in a separate thread, preventing UI issues.
WebSocket Connection: Establish a persistent WSS connection to the Gemini Live API (gemini-live-2.5-flash-native-audio).
Audio Format: PCM audio is sent/received as base64 encoded chunks, with 16kHz for input and 24kHz for output.
Workflow:
Capture: getUserMedia captures microphone data.
Process: AudioWorkletProcessor resamples/buffers audio.
Stream: Send base64 chunks via WebSocket.
Respond: Receive audio PCM and use AudioWorklet to play it. 
YouTube
YouTube
 +4
Implementation Resources
Live API Examples: Explore GitHub examples for WebSockets and audio handling.
Web Console Demo: Use the live-api-web-console as a reference hook for React applications.
Best Practices: View the colab notebook for setting up the WebSocket connection. 
Google Cloud Documentation
Google Cloud Documentation
 +1
