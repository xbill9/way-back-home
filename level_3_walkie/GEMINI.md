# ADK & Gemini 3.1 Flash Live Developer Guide

Do not ever use any 2.0 models they are depreciated.
Suggest only 2.5 models or later

The video frame rate is **1 per second**. This file used to say 2, which
contradicted the page it cites below: the Live API capabilities guide states
video frames are sent "as individual images (e.g., JPEG or PNG) at a specific
frame rate (max 1 frame per second)". 2 FPS did work -- nothing rejects the
surplus frames -- but they are billed and consume the audio+video session
budget twice as fast. `VIDEO_FPS` defaults to 1.0 in both `main.py` and
`agent.py`, and is honoured up to 5.0. It was briefly a hard ceiling in both,
so raising it did nothing -- silently, which is what made that a bug rather than
a policy. Raising it does not buy accuracy either: measured with
`scripts/scan_accuracy.py` (60% of frames blurred, ten trials per condition),
1.0 scored 10/10 with a 0.68s median and 2.0 scored 10/10 with 1.80s. The two clamps must stay identical,
since `agent.py` interpolates the value into the instruction.

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

-   **Model ID:** `models/walkie-talkie` (default in this project since the Live audio EAP -- see "Live audio EAP" below). `gemini-3.1-flash-live-preview` is what it replaced, and the specifications in this section still describe that model; the EAP model cards are not published.
-   **Context Window:** 128K tokens (Input) / 64K tokens (Output).
-   **Modality:** Natively multimodal. Supports Text, Image, Audio, and Video as input; Text and Audio as output.
-   **Audio architecture:** *Unresolved.* This file has claimed "native audio ... without external TTS/STT engines" while the `RESPONSE_MODALITY` comment in `main.py` calls the model half-cascade. Neither the model card nor the Live API guide states which is true -- the guide does not mention `gemini-3.1-flash-live-preview` at all yet. Circumstantially the half-cascade reading fits, since proactive audio and affective dialogue are native-audio features and 3.1 Flash Live supports neither, but that is inference. Do not rely on either claim; nothing in this project's behaviour depends on it.
-   **Real-time Streaming:** Optimized for continuous data streams (video/audio) with "immediate" response latency.

### Use Case in This Project

The Biometric Security System leverages Gemini 3.1 Flash Live's **video streaming** and **complex function calling** capabilities to:
1.  Analyze a live video feed of hand gestures.
2.  Maintain a robotic, low-latency conversational persona.
3.  Execute the `report_digit` tool immediately upon visual verification of a gesture.
4.  Execute the `trigger_system_error` tool if an offensive gesture (middle finger) is detected.
5.  Execute the `trigger_heavy_metal_mode` tool if the "Devil's Horns" gesture is detected (secret override).

## Working with ADK (Agent Development Kit)

The backend uses the Google ADK to orchestrate the agent's behavior and tools.

### Gemini 3.1 Compatibility (native as of google-adk 2.6.3)

Earlier revisions of this project shipped a monkey-patching utility (`backend/app/patch_adk.py`) to work around the `media_chunks` deprecation in Gemini 3.1. **That file has been removed** — ADK 2.6.3 handles it natively:
- **`media_chunks` deprecation**: `GeminiLlmConnection.send_realtime` detects Gemini 3.x Live via `_is_gemini_3_x_live()` and sends `audio=` / `video=` directly instead of the legacy `media=`.
- **Text input**: `_send_content` routes a single-part text `Content` to `send_realtime_input(text=...)` for 3.x models.

One caller-side consequence: `LiveRequestQueue.send_realtime()` accepts only `types.Blob`. Text stimuli (the initial handshake, user text, and the `CONTINUE_SURVEILLANCE` heartbeat) go through `send_content()` via the `send_text_stimulus()` helper in `main.py`.

Not carried over: the old patch also guarded `AudioCacheManager.cache_audio` against `NoneType` blobs. Upstream still calls `len(audio_blob.data)` unguarded, so a blob with `data=None` would raise — no such path exists in this app's code today.

### ADK 2.x status

**Nothing in this project is deprecated or broken by the 2.0 changes**, and that is checkable rather than assumed:

```bash
python -W error::DeprecationWarning -m pytest -q   # zero ADK warnings
```

The only warning that surfaces is Starlette's `httpx` notice from the test harness, which has nothing to do with ADK. Re-run that after any ADK bump.

The 2.0 breaking changes all target patterns this project does not use, which is why the upgrade was a non-event:

| 2.0 breaking change | Why it does not apply here |
|---|---|
| Agents subclass `BaseNode`; custom `_run_async_impl()` / `generate_content()` overrides are **silently bypassed** | Plain `Agent`, no subclass, no overrides |
| Manually appending events to the session breaks graph determinism | Events are never appended by hand; `run_live()` owns the session |
| Event schema gained `node_info` / `output`; rigid custom session-storage schemas need migrating | `InMemorySessionService` — no schema |
| Broad `except` masks the framework's retry, and `except BaseException` breaks Human-in-the-Loop pausing | The broad handlers in `main.py` sit in `upstream_task` and the lifecycle wrapper — app level, outside the graph engine |

Adopted from 2.x:

- **`Runner(..., auto_create_session=True)`**. `run_live()` already calls `_get_or_create_session()` internally; without the flag a missing session is a `ValueError`, which is why this used to hand-roll get-then-create. That block is gone.

Deliberately **not** adopted:

- **`Runner(app=App(...))`** is described as "the recommended way to create a runner", but `Runner(agent=..., app_name=...)` is explicitly still supported and is wrapped into an `App` internally, with no deprecation warning. Churn without benefit.
- **`plugins=[...]`**, the new extension point. Nothing to plug in.

Deprecated in 2.6.3 and correctly avoided: **`run_live(session=...)`** — pass `user_id` / `session_id`, which this does.

Unused `RunConfig` knobs that are genuinely relevant if the demo needs them:

- **`realtime_input_config`** — VAD tuning (`startOfSpeechSensitivity`, `endOfSpeechSensitivity`, `prefixPaddingMs`, `silenceDurationMs`). Automatic VAD is the default and is used implicitly. These are the dials if barge-in ever feels too eager or too slow.
- **`save_live_blob`** / **`save_live_audio`** — persist live media to the artifact service. Off by default; a real debugging aid for a session that is hard to reproduce.
- Token usage rides on **usage-metadata events** yielded by `run_live()`. Nothing reads them.

Two traps worth knowing:

- **`RunConfig.save_live_model_audio_to_session` does not exist.** ADK's own `run_live()` docstring names it; the real fields are `save_live_blob` and `save_live_audio`. Do not code against the docstring.
- **The test suite cannot catch session-lifecycle regressions.** `tests/` stubs `runner.run_live()`, so session creation, resumption and teardown are never exercised — `make test` passes whether or not they work. Changes in that area have to be verified against the real API with a WebSocket client.

### Agent Definition (`backend/app/biometric_agent/agent.py`)

Agents are defined using the `Agent` class, which encapsulates the model, instructions, and tools.

```python
from google.adk.agents import Agent

root_agent = Agent(
    name="biometric_agent",
    # A model id, or a Gemini instance when ADK's registry cannot resolve the
    # id -- which is the case for both EAP models. See live_models.py.
    model=build_live_model(MODEL_ID),
    generate_content_config=build_generate_content_config(MODEL_ID),
    tools=_build_tools(),
    instruction="..."
)
```

### Tool Implementation

Tools are standard Python functions with clear docstrings. Gemini uses these docstrings to understand when and how to call the tool.

-   **`report_digit(count: int)`**: Sends the detected finger count (1-5) to the system.
-   **`trigger_system_error()`**: Triggers a fatal error if an offensive gesture (middle finger) is detected.
    -   *Enforcement Detail*: The backend immediately terminates the WebSocket connection after sending the error signal to prevent further interaction.
-   **`trigger_heavy_metal_mode()`**: Activates the "Heavy Metal Authentication Override" if the "Devil's Horns" gesture is detected (index and pinky extended).
    -   *Implementation Detail*: The frontend (`BiometricLock.jsx`) triggers a custom audio event when this tool is executed, playing the "War Pigs" intro from a verified `archive.org` source.
-   **Critical Requirement:** Tool results should be handled as specified in the agent's instructions (e.g., "When you get the result of `report_digit`, DO NOT SPEAK").

### Runner and Session Service (`backend/app/main.py`)

The `Runner` connects the agent to the FastAPI application and manages the execution loop. The `InMemorySessionService` tracks state across multiple turns in a session.

### Model Selection & Fallback

The system selects the model ID based on the execution context (`live_models.get_model_id()`):
-   **Default**: `models/walkie-talkie` (Optimized for WebSockets/Live API).
-   **Override**: `MODEL_ID`. Bare EAP names are normalised, so `MODEL_ID=walkie-talkie` becomes `models/walkie-talkie` rather than 404ing at connect.
-   **Fallback**: `gemini-2.5-flash` is used automatically when running via `adk run` (CLI) to avoid 404 errors, since every Live-only model rejects `generateContent`. The check is "is this model Live-only", not "is this model the default" -- an explicit `MODEL_ID` pointing at another Live model used to sail past it into the 404 it exists to prevent.

### WebSocket Integration & Proactivity

ADK provides a bidirectional streaming interface over WebSockets.

-   **Audio Config**: `response_modalities` is `["AUDIO"]`. On the EAP models that is not a choice -- audio is the only supported response modality, and `main.py` forces it with an error log if `RESPONSE_MODALITY=TEXT` is set. Output audio transcription is the supported way to get text.
-   **Proactivity**: **Proactive audio is permanently on for the EAP models**, and passing `proactiveAudio: false` is a hard error, so `ProactivityConfig` stays unset. (`gemini-3.1-flash-live-preview` was the opposite: not proactive at all, and it would not initiate speech or tool calls until it received input.) A proactive model may decline to answer a stimulus it judges irrelevant, which is worth remembering when the heartbeat appears to be ignored.
-   **Neural Handshake**: The backend sends a "Neural handshake" text stimulus immediately after connection to "wake up" the model.
-   **Heartbeat Stimulus**: To prevent the model from idling during long periods of visual-only surveillance, a `CONTINUE_SURVEILLANCE` text stimulus is sent every 10 seconds if no other input is detected.
-   **Opening line**: comes from the model, not from a recording. This section used to document a "Manual Greeting" -- `mock/mock_audio.pcm` read off disk and sent wrapped in a synthetic `serverContent.modelTurn`, indistinguishable from real model output. It was removed; `tests/test_ws_session.py` now pins its absence. The "Neural handshake" stimulus forces the first turn and the agent instruction ends with `Say "Scanner Online." to initialize.`
-   **Interruption**: on an `interrupted` event the client clears the playback queue, per the Live API guidance to "stop playing audio and clear queued playback".
-   **Session length**: `context_window_compression` is enabled in `RunConfig`. Without it an audio+video session is capped at 2 minutes (audio-only gets 15), which a single demo run can reach.

## Live audio EAP: walkie-talkie and clever-chatter

Source: the EAP invitation "Gemini API Early Access Program | Walkie-Talkie and
Clever-Chatter" and its follow-up on `interaction_status`. There are no public
docs and no model cards; everything in this section comes from those two mails,
except where it says "verified", which means verified against ADK 2.6.3 /
google-genai 2.17.0 in this repo.

| | `models/walkie-talkie` | `models/clever-chatter` |
| --- | --- | --- |
| Architecture | Latency-optimized | Higher reasoning |
| Background thinking | Not supported | `thinking_config` |
| Proactive audio | Always on | Always on |
| Client content | Whole session | Whole session |
| Default function calling | `NON_BLOCKING` | `NON_BLOCKING` only |
| `BLOCKING` function calls | Supported (back-compat) | Hard error |
| Function scheduling | `SILENT` / `WHEN_IDLE` / `INTERRUPTED` | Not supported |
| Response modality | AUDIO only | AUDIO only |

`walkie-talkie` is this project's default: it is the drop-in successor to
`gemini-3.1-flash-live-preview`, and counting fingers does not need background
reasoning. `clever-chatter` is selectable with `MODEL_ID` and gets a
`thinking_config` (`THINKING_LEVEL`, default `MINIMAL`); nothing else differs.

**Access is per GCP project.** The API key must come from a project allowlisted
for the EAP or the socket simply refuses to open. Here that is **`comglitn`
(project number `1056842563084`)**, and the key is the one named `EAP` in that
project, restricted to `generativelanguage.googleapis.com`. `~/gemini.key` holds
it; the previous key (from `aisprint-491218`, which is *not* allowlisted) is at
`~/gemini.key.pre-eap-backup`. `scripts/verify_setup.sh` warns when the active
project is not the EAP one, because "connection refused" is otherwise
indistinguishable from a dozen other failures -- but note the check sees the
*active gcloud project*, not the key's project, and those can legitimately
differ.

### Protocol changes this project had to absorb

-   **`turnComplete: true` no longer means idle.** With asynchronous function
    calling, tool calls and audio frames can arrive after it. `main.py` never
    read `turn_complete`, so no client state machine needed rewriting -- but the
    requirement it *does* impose is that ADK hand over tool calls as they
    arrive. See "ADK gaps" below.
-   **`interaction_status`** (`IN_PROGRESS` / `REQUIRES_ACTION`) is the
    definitive idle signal, shipped in a private SDK build. **It cannot be
    surfaced through ADK today**, and installing that build does not change
    that: ADK rebuilds every `LiveServerMessage` into an `LlmResponse`, which is
    a pydantic model with `extra="forbid"` and no such field, so the value is
    dropped before this application sees it (verified). The private wheel is
    also `google_genai-2.14.0`, i.e. a downgrade from the pinned 2.17.0, and it
    is served over mTLS, so it cannot be installed in the Docker build at all.
    Nothing here depends on the field; it is only worth having once ADK carries
    it.
-   **Affective dialogue** is removed from the API. Never set
    `enable_affective_dialog`.
-   **Proactive audio** is permanently on; `proactiveAudio: false` is a hard
    error. Never set `proactivity`.
-   **Turn coverage** defaults to `TURN_INCLUDES_AUDIO_ACTIVITY_AND_ALL_VIDEO`
    (carried over from 3.1 Flash Live, not new): every frame sent is a frame
    billed and a frame in the context window. The `VIDEO_FPS` clamp (1.0) is the
    only thing bounding that here.
-   **`send_client_content` works for the whole session**, not just to seed
    history, and with `turn_complete=true` it interrupts generation
    unconditionally. Realtime text is the VAD-respecting alternative and is what
    the stimuli in `main.py` use.

### Async function calling

Declaring `response_scheduling` on a tool is what makes ADK mark its declaration
`NON_BLOCKING` (`_mark_live_async_tools_non_blocking()` in
`flows/llm_flows/base_llm_flow.py`) and stamp the scheduling onto the
`FunctionResponse`. All three tools here use `SILENT`: the result is fed back
without triggering a model turn, which is the protocol-level version of the
instruction's "after receiving the tool result: STAY SILENT", and it means the
scanner speaks without waiting on the tool round-trip.

This is conditional on the model (`agent.py: _build_tools()`), because
`gemini-3.1-flash-live-preview` is the exact inverse: `BLOCKING` only, with
`NON_BLOCKING` unsupported. Setting scheduling unconditionally would break the
pre-EAP model.

### ADK gaps

ADK 2.6.3 has no knowledge of these model ids, and both gaps are name-based
(verified by reading `google/adk/utils/model_name_utils.py`):

1.  `LLMRegistry` resolves `gemini-*` and nothing else, so
    `Agent(model="models/walkie-talkie")` raises *"Model models/walkie-talkie
    not found"* at import. Fixed by passing a `Gemini` **instance** instead of a
    string -- `Agent(model=...)` accepts `str | BaseLlm`.
2.  `_is_gemini_3_x_live()` means "starts with `gemini-3.` and contains
    `-live`", and it gates three behaviours in `GeminiLlmConnection`: the split
    `send_realtime_input(audio=/video=)` fields, yielding tool calls
    immediately instead of buffering them until `turn_complete`, and treating
    input transcription as a single final event. The second is the one that
    matters most -- with async function calling, a `report_digit` arriving after
    `turnComplete` would otherwise sit in a buffer until the next turn.

`live_models.EapLiveGemini` closes gap 2 by setting the flag on the connection
ADK just built. It is a subclass, not a patch: nothing in ADK is rebound and
non-EAP models never reach the class. Delete it when ADK learns these ids.

### What is verified, and how

`make test` stubs `run_live()`, so the suite proves only that the right config is
*assembled*. Whether the endpoint accepts it was checked by hand on 2026-08-12,
against `models/walkie-talkie` with the EAP key:

-   `test_live_connection.py` -- both EAP endpoints connect and return a
    `LiveServerMessage`. The SDK-level check, no ADK involved.
-   Full backend (`make run`) + a WebSocket client -- the model opens with
    "Scanner Online." through `EapLiveGemini`, so the forced Gemini 3.x realtime
    routing is accepted, not merely tolerated.
-   Tool path -- a stimulus describing three fingers produced
    `[FUNCTION CALL] report_digit({'count': 3})` 410 ms later, the tool body ran
    server-side, and the client received **exactly one** `{"type":"match"}`
    frame. The model said nothing after the tool result, which is what `SILENT`
    scheduling is for.

Not yet exercised against the endpoint: `clever-chatter` beyond a bare connect
(no `thinking_config` session has been run), the video path on the EAP model
(the tool check used a text stimulus, not a JPEG frame), and session resumption,
which has never worked here anyway.

**Access is the thing that bites.** Before the EAP key was installed, both
endpoints were refused with `1008 models/walkie-talkie is not found for API
version v1alpha, or is not supported for bidiGenerateContent` -- on `v1alpha` and
`v1beta` alike, while `gemini-3.1-flash-live-preview` connected fine from the
same key. That error is what a non-allowlisted project looks like; it is not a
transport or API-version problem. (ADK already uses `v1alpha` for the API-key
path -- `Gemini._live_api_version`.)

## Developer Workflow

1.  **Instruction Tuning:** Modify the `instruction` string in `agent.py` to refine the scanner's behavior and personality.
2.  **Tool Expansion:** Add new functions to the `tools` list in `agent.py` to expand the system's capabilities.
3.  **Local Testing:** Use `mock.sh` to test the frontend and backend orchestration without consuming Gemini API credits for every run.
4.  **Automated Testing**: Run `make test`. Note that async tests (like `test_live_connection.py`) require the `@pytest.mark.anyio` marker and the `anyio` plugin.
5.  **Deployment:** Ensure all environment variables (especially `MODEL_ID` and `GOOGLE_API_KEY`) are correctly set in the Cloud Run configuration.
    -   **Manual Deployment**: Use `make deploy` to deploy directly from your local environment.
    -   **Automated Deployment**: Use `cloudbuild.yaml` for a managed CI/CD pipeline:
        ```bash
        gcloud builds submit --config cloudbuild.yaml --substitutions=_GOOGLE_API_KEY=YOUR_KEY
        ```

## Migrating from Gemini 2.5 Flash Live

The migration this project is actually on is 3.1 Flash Live to
`models/walkie-talkie` -- see "Live audio EAP" above. This section is kept for
the 2.5 to 3.1 step, which the notes below still describe accurately.

Gemini 3.1 Flash Live Preview is optimized for low-latency, real-time dialogue.

- **Model string:** Update from `gemini-2.5-flash-native-audio-preview-12-2025` to `gemini-3.1-flash-live-preview`.
- **API Call migration:** `session.send` is deprecated. Use `session.send_realtime_input(text="...")` or `session.send_realtime_input(audio=...)` instead. (Verified in `test_live_connection.py`).
- **Thinking configuration:** Gemini 3.1 uses `thinkingLevel` (default: minimal) instead of `thinkingBudget`.
- **Server events:** A single event can contain multiple content parts (audio chunks + transcript).
- **Proactive audio:** Not yet supported in Gemini 3.1 Flash Live. Remove `ProactivityConfig` from your code and use stimuli to trigger model responses.

## Current System Status

- **Tests:** 57 passing (`make test`) -- hermetic, no API key, no network.
- **Linting:** 100% compliant with Ruff (Python) and ESLint (Frontend) (`make lint`).
- **Performance:** 1 FPS video stream using `toBlob` at 0.6 quality. The capture loop is a `setTimeout` chain, not `requestAnimationFrame` -- rAF is throttled to zero in a backgrounded tab, which silently killed video while audio kept streaming.

## Reference sources

Checked 2026-08-12 against ADK 2.6.3 (the pinned version). Each claim in this
file that came from a doc names the page it came from, so a future reader can
recheck rather than re-derive.

### Gemini Live API

-   [Live API overview](https://ai.google.dev/gemini-api/docs/live-api) -- WebSocket transport, PCM formats (16 kHz in / 24 kHz out), ephemeral tokens. The ephemeral-token advice targets clients that connect **directly** to Google; this project's browser talks only to its own FastAPI backend, with the key server-side in Secret Manager, so it does not apply here.
-   [Live API capabilities](https://ai.google.dev/gemini-api/docs/live-api/capabilities) -- **source of the 1 FPS video ceiling** ("max 1 frame per second"), JPEG/PNG frame format, and `mediaResolution` (e.g. `MEDIA_RESOLUTION_LOW`, currently unset here). Also names `input_audio_transcription` / `output_audio_transcription` as *config* fields, the distinction the transcript bug turned on. Automatic VAD is the default and this project never configures it; if barge-in ever needs tuning the knobs are `automaticActivityDetection.startOfSpeechSensitivity` / `endOfSpeechSensitivity`, `prefixPaddingMs` and `silenceDurationMs`. Per-session token usage is available on `usageMetadata`, which nothing here reads.
-   [Session management](https://ai.google.dev/gemini-api/docs/live-session.md.txt) -- **source of the session caps**: "audio-only sessions are limited to 15 minutes, and audio-video sessions are limited to 2 minutes", and compression "extends sessions to an unlimited amount of time". Also: resumption handles must be retained by the client and stay valid 2h. This project sets `SessionResumptionConfig()` but stores no handle, so resumption is configured, not working.
-   [Model card: gemini-3.1-flash-live-preview](https://ai.google.dev/gemini-api/docs/models/gemini-3.1-flash-live-preview) -- 131,072 in / 65,536 out; `thinkingLevel` defaults to `minimal` (unset here, which is the right default for latency); function calling is synchronous only; "these features are not yet supported" for proactivity and affective dialogue.
-   EAP mail, "Gemini API Early Access Program | Walkie-Talkie and Clever-Chatter", plus its `interaction_status` follow-up -- **the only source** for everything in "Live audio EAP" above. No public page documents these models; AI Studio has them at `https://aistudio.google.com/live?model=walkie-talkie` and `?model=clever-chatter`.
-   [Docs index](https://ai.google.dev/gemini-api/docs/llms.txt) -- fetch `*.md.txt` variants for plain-text pages.
-   `.gemini/skills/live/SKILL.md` in this repo -- the fullest single reference, and ahead of the public guide, which still centres on 2.5 and does not mention 3.1 Flash Live.

### ADK

-   [Python API reference](https://adk.dev/api-reference/python/) -- the authority for event shapes. **`Event` exposes `inputTranscription` / `outputTranscription` / `interrupted`**; `input_audio_transcription` is the `RunConfig` knob that enables transcription, *not* the field on the event it produces. Reading the config name off the event is exactly the bug that made transcript logging a no-op here for the life of the project.
-   [RunConfig](https://adk.dev/runtime/runconfig/) -- `context_window_compression` defaults to `None`, so the 2-minute cap applies unless you ask for it.
-   [Live/streaming dev guide](https://adk.dev/streaming/) -- five-part series; part 3 covers event handling. Note `google.github.io/adk-docs/...` 301s to `adk.dev`.
-   [ADK 2.0 release notes](https://adk.dev/2.0/) -- the breaking-change list behind the "ADK 2.x status" table above: graph-based workflows, the `BaseNode` execution model, the event-schema additions, and the exception-handling contract.
-   `run_live()`'s own docstring is the reference for **which events are yielded versus saved**: partial *and* non-partial transcription events are yielded, but only non-partial ones are saved to the session. That is why the transcript logging gates on `finished` -- it selects exactly one line per turn instead of one per partial.
-   The installed package is the tiebreaker when docs are vague or unreachable: `google/adk/models/gemini_llm_connection.py` shows `_send_content` routing single-part text to `send_realtime_input(text=...)` for Gemini 3.x (which is why `send_content()` here is correct despite the "don't use client content" guidance), and `send_realtime` routing blobs to `audio=` / `video=` rather than the deprecated `media=`.

### Model card

-   [Gemini 3.1 Flash Live Model Card](https://deepmind.google/models/model-cards/gemini-3-1-flash-live/)


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
