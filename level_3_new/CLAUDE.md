# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

Biometric Security System — a FastAPI + Google ADK backend streaming video/audio to the Gemini Live API, with a React/Vite frontend.

Commit directly to `main` and push — no feature branches, no PRs. Deps install into the active interpreter via `./scripts/install_deps.sh`; no virtualenv.

## Support scripts

Every `.sh` entrypoint starts with `#!/bin/bash`, `set -euo pipefail`, and `cd "$(dirname "${BASH_SOURCE[0]}")"`, so it operates on **this** directory no matter where it is invoked from. (They used to hardcode `cd $HOME/way-back-home/level_3_gemini` and act on the sibling copy.) Keep that preamble on any new script — the `cd` in particular, since an unguarded one used to let `build.sh` overwrite a `Dockerfile` in the caller's working directory.

Secrets are never passed on a command line or written world-readable: `~/gemini.key` and both generated `.env` files are `chmod 600`, and anything that prints `.env` pipes it through a `sed` redaction.

## Commands

- `make test` — `python -m pytest`. See "Tests can't fail" below.
- `make lint` — `ruff check .`, `ruff format --check .`, then `cd frontend && npm run lint`. Check-only; there is no `make fmt`. Run `ruff format .` to actually format.
- `make frontend` — `npm ci && npm run build` (**`ci`**, not `install`: the Dockerfile builder does the same, so the image and your machine build the identical locked tree. Run `npm install` by hand when you mean to bump `package-lock.json`). Required before the backend can serve the SPA; `main.py` mounts `frontend/dist` at `/` and only warns if it's missing. `frontend/node_modules` is absent on a fresh clone, so `make lint` fails until this runs.
- `make run` (8080, real API) · `make mock` (8080, offline fake server) · `make adk` (8000, ADK dev UI) · `make testadk` (interactive ADK CLI).
- `make verify` — `scripts/verify_setup.sh`; checks project, APIs, Python deps, and both `.env` files. Exits non-zero on failure, so it can gate a build.
- `make build` / `make deploy` delegate to `./build.sh` / `./deploy.sh` — one definition each, no duplicated gcloud invocation in the Makefile. Both derive `PROJECT_ID` from `~/project_id.txt` and default `SERVICE_NAME`/`REGION`/`IMAGE_PATH`; override via the shell. `make endpoint` shares the same `SERVICE_NAME`/`REGION` defaults.

Use `make mock` for frontend work — it replays `mock/mock_audio.pcm` and a canned tool call, so it costs nothing.

## Models

Never use 2.0 models (deprecated); 2.5 or later only. Production is `gemini-3.1-flash-live-preview`. `agent.py: get_model_id()` inspects `sys.argv` and falls back to `gemini-2.5-flash` under `adk run`, because the Live preview model 404s on `generateContent` — so `make testadk` exercises a different model than production. Live-model access is allowlisted; `./testmodels.sh` lists models supporting `bidiGenerateContent`.

## No monkey patching (removed)

`backend/app/patch_adk.py` is gone. ADK 2.6.3 handles Gemini 3.x Live natively — it detects the model via `_is_gemini_3_x_live()` and routes audio to `send_realtime_input(audio=)`, images to `video=`, and single-part text Content to `text=`. Do not reintroduce the patches; fix the caller instead.

The one thing this changed for callers: **`LiveRequestQueue.send_realtime()` accepts only `types.Blob`.** Text must go through `send_content()` — use the `send_text_stimulus()` helper in `main.py`. Passing a bare string raises a Pydantic `ValidationError` (the old patch hid this with `model_construct`).

## ADK 2.x compatibility

Pinned at `google-adk==2.6.3`. Nothing here is deprecated, and that is checkable: `python -W error::DeprecationWarning -m pytest -q` reports zero ADK warnings — re-run it after any ADK bump.

Four rules keep it that way. 2.0 moved agents onto a graph engine, so:

- Use the plain `Agent`. Custom `BaseNode` subclasses and `_run_async_impl()` / `generate_content()` overrides are **silently bypassed** — no error, just no effect.
- Never hand-append events to the session; `run_live()` owns it.
- The Runner sets **`auto_create_session=True`**. Do not reintroduce a hand-rolled `get_session`-then-`create_session`.
- Pass `user_id`/`session_id` to `run_live()`. The `session=` parameter is deprecated.

`RunConfig.save_live_model_audio_to_session` **does not exist**, despite ADK's own `run_live()` docstring naming it; the real fields are `save_live_blob` / `save_live_audio`. Full breaking-change table in `GEMINI.md` → "ADK 2.x status".

## Environment

`GOOGLE_API_KEY`, `GEMINI_API_KEY`, and `GEMINI_KEY` must all be set to the same value — every config path sets all three. `GOOGLE_GENAI_USE_VERTEXAI=False` (Gemini API key path, not Vertex, despite the GCP setup). Secrets live outside the repo in `~/project_id.txt` and `~/gemini.key`; `runadk.sh` generates `backend/app/biometric_agent/.env` from them. There is no `.env.example`.

Installing deps: **always go through `./scripts/install_deps.sh`** (`--dev` also installs `requirements-dev.txt`). A bare `pip install -r requirements.txt` or `uv pip install -r requirements.txt` hard-fails with `ResolutionImpossible` / `No solution found`.

`requirements.txt` pins `websockets==17.0.1`, above the caps `google-adk` (`>=15.0.1,<16`) and `google-genai` (`>=13.0.0,<17`) declare. Those bounds are "last version we tested", not a real incompatibility — every websockets API these libraries call exists in 17.0.1 — so `overrides.txt` overrides them. `uv` applies it with `--override`; pip has no equivalent (`--constraint` can only tighten a bound), so the script installs the tree without websockets and then forces the pin with `--no-deps`. Both routes resolve to the same 56-package set.

Do **not** "fix" this with a bare `pip install -r requirements.txt --no-deps` — `--no-deps` applies to the whole command and skips every transitive dependency (pydantic, starlette, sqlalchemy…), leaving an app that can't import. `pip check` reporting the websockets conflict afterwards is expected.

`Dockerfile` passes `--override overrides.txt`, and `init.sh` calls the script, so `make build`, `cloudbuild.yaml`, and a fresh `./init.sh` all work. `init.sh` no longer runs `pip install google-adk --upgrade`: that ignored the `google-adk==2.6.3` pin and silently re-resolved `websockets` back below 16. If the Live socket misbehaves, drop the override and pin `websockets==15.0.1` to test whether it's the cause.

`main.py` hard-exits if `GOOGLE_API_KEY` is unset **only under `python main.py`** — the `sys.exit(1)` sits behind `if __name__ == "__main__"`. On import it logs two CRITICAL lines and continues (the comment promises it "raises error" otherwise; nothing does), which is what makes the offline test suite possible. `PORT` reads Cloud Run's `$PORT` (default 8080). `VIDEO_FPS` (default 1.0 — the Live API's documented max — honoured up to 5.0) is interpolated into the agent instruction, so backend and prompt stay in sync; `main.py` and `agent.py` declare it separately and the two ranges must stay identical. It was briefly **hard-clamped** at 1.0, so `VIDEO_FPS=2` silently did nothing; that is fixed, because a knob that ignores you is worse than no knob. Raising it does not buy accuracy — `scripts/scan_accuracy.py` scored 10/10 at both 1.0 and 2.0 under identical blur, with 1.0 the *faster* of the two (0.68s vs 1.80s median). Raise it only with a measurement. `HEARTBEAT_INTERVAL` defaults to 10.0, clamped 5.0–30.0.

Four more knobs, all read once at import:

- `RESPONSE_MODALITY` (`AUDIO` default, or `TEXT`) — the modality used to be *inferred* from `"live" in model_name`, which was right for `gemini-3.1-flash-live-preview` only by coincidence (it is half-cascade, not native-audio). A future model name without the substring would have silently muted the demo. It is a deployment decision now.
- `VIDEO_WIDTH` / `VIDEO_HEIGHT` / `JPEG_QUALITY` (640×480, q60) — capture size and quality, **shipped in the `config` frame** so the client has no second copy, and tunable without rebuilding the frontend. `scripts/scan_accuracy.py` measured 5/5 at every setting — 640×480 q60 at 128.6 kbit/s, 480×360 q50 at 77.3, 320×240 q40 at 40.1 — and **that table got the default wrong**: 480×360 shipped on it and made real accuracy visibly worse. The fixtures all have a hand filling the frame, so shrinking costs nothing there, while a hand at arm's length from a laptop loses its fingers first. **Don't trade resolution for bandwidth** — the uplink is ~77% microphone (256 kbit/s of raw PCM, uncompressible), so the savings are in the audio gate, and resolution is what accuracy is made of.
- `ALLOWED_ORIGINS` — comma-separated allowlist checked at the WebSocket handshake. **CORS does not apply to WebSockets**, so this is the only gate in front of a public `--allow-unauthenticated` URL. Empty means accept anything and logs a warning at startup; `deploy.sh` passes it through and warns again if you deployed without one.
- `HEARTBEAT_ENABLED` (default on) — the heartbeat sends `CONTINUE_SURVEILLANCE` as a **real user turn**, not a transport keepalive, so the model can answer it out loud. It only fires when the client has gone quiet.
- `PORT` — see above.

**There is no canned audio greeting.** `main.py` used to open every session by reading `mock/mock_audio.pcm` and sending it wrapped in a synthetic `serverContent.modelTurn`, which is indistinguishable from real model output to the client — a recording presented as the Live API, in a demo about the Live API. It also only existed locally, since the Dockerfile never copies `mock/`. The opening line now comes from the model: the `"Neural handshake"` stimulus forces the first turn and the agent instruction ends with `Say "Scanner Online." to initialize.` `tests/test_ws_session.py` pins this — don't reintroduce it.

## Tests

`pytest.ini` sets `testpaths = tests backend/app/biometric_agent`, so `make test` collects only the hermetic suites: **23 tests, no API key, no network, no charge — and they can fail.** Report a green `make test` as verification of the protocol layer only.

Specifically, the suites stub `runner.run_live()`, which means **session creation, resumption and teardown are never exercised** — `make test` passes whether or not they work. Anything touching the session lifecycle or `RunConfig` has to be checked against the real API with a WebSocket client; that is how `auto_create_session` and `context_window_compression` were verified.

`tests/test_ws_protocol.py` covers the frame-by-frame wire contract; `tests/test_ws_session.py` covers whole-connection behaviour — origin policy, the audio-rate handshake, clean shutdown, and the two regressions above.

**`scripts/scan_accuracy.py` is the only thing here that measures the model rather than the plumbing**, and it needs no human: it drives the real endpoint with the fixture hands in `tests/fixtures/hands/` (640×480 JPEG q60, the exact format the browser sends), a `{"type":"text"}` frame in place of saying "scan", and scores every `report_digit` against a known count. One billed session per run. `--blur-prob`/`--jitter` approximate a real webcam; `--min-rate` makes it a gate. It sends **no audio**, so nothing involving VAD or a microphone is in its reach. The fixtures were generated with `gemini-3.1-flash-lite-image` and **every count was verified by eye before committing** — generate the input, never the expectation. See `@docs/testing-strategy.md` → Tier 6.

The **root-level** `test_*.py` files are excluded from default collection and are manual smoke checks, not tests. Each wraps its body in `try/except Exception: print(...)`, so it passes no matter what. `test_ws_backend*.py` need a server already on `ws://127.0.0.1:8080`; `test_live_connection.py` makes a **real billed Live API call** and builds its `genai.Client` at *import*, which is why it used to abort collection for the whole session. Run them by explicit path when you actually want them.

`tests/` drives the real WebSocket endpoint in-process: `main.py` imports fine with no key (the `sys.exit(1)` is guarded by `if __name__ == "__main__"`), and `runner.run_live()` is the only call that reaches Gemini, so stubbing it makes the whole endpoint testable offline. See `@docs/testing-strategy.md` before adding tests — it documents the fixtures, what's deliberately not covered, and the shutdown bug that forces `ws_connect` to suppress exceptions on teardown.

## Style

`ruff.toml` in **this directory** (not the git root, `~/way-back-home` — that scoping is what keeps the sibling `level_3*/` copies out of the rule set) pins the rule set. Both ruff steps of `make lint` pass — **a red ruff run is now a real regression**, not pre-existing noise. Read the comments in `ruff.toml` before widening `select` or removing an `ignore`; each entry records why. In short: `E4/E7/E9`, `F`, `I`, `UP`, `B`, `ASYNC`, `SIM`, `RUF`, minus `ASYNC230`/`ASYNC240` (one-shot startup reads, not per-request I/O) and `SIM102` (defensive `hasattr` probing on ADK event shapes). `E501` and `W` are left out because `ruff format` owns line length and whitespace, and agent.py's instruction prompt is one long string literal no formatter can wrap.

The exclude that matters: **`extend-exclude = ["*.md"]`**. Ruff 0.16 formats Python fenced blocks inside Markdown, so without it a bare `ruff format .` rewrites the code samples in `GEMINI.md`, `.gemini/skills/live/SKILL.md`, and `docs/article-*.md`. With it, `ruff format .` is safe.

The third `make lint` step is `cd frontend && npm run lint`, which needs `frontend/node_modules` — run `make frontend` first or that step fails on a fresh clone.

`.claude/settings.json` runs a `PostToolUse` hook that `ruff format`s any `.py` file after a Write/Edit, so Python files change on disk after an edit.

The one non-default JS rule is `frontend/eslint.config.js`: `'no-unused-vars': ['error', { varsIgnorePattern: '^[A-Z_]' }]`. No TypeScript, no Prettier.

## Other gotchas

- `Dockerfile` is checked in and is the source of truth. `build.sh` used to regenerate it from a heredoc on every run, silently discarding edits; it no longer does.
- **Deploys read the API key from Secret Manager**, not from `--set-env-vars`. `deploy.sh` creates/updates the `gemini-api-key` secret from `~/gemini.key`, grants the runtime service account `secretAccessor`, and wires it in with `--set-secrets`; `cloudbuild.yaml` does the same via `_SECRET_NAME`. Never reintroduce the key as a plaintext env var or build substitution — both are readable after the fact.
- `--set-env-vars` **replaces the entire environment**, so all variables must go in one comma-separated flag. Repeating the flag keeps only the last occurrence; the old `deploy.sh` passed seven and therefore deployed only `MODEL_ID`.
- `.gcloudignore` takes full precedence over `.gitignore` for build uploads — `.gitignore`'s `.env` rule does **not** apply. `.env`/`*.key` are excluded explicitly there and in `.dockerignore`; without that, `runadk.sh`'s agent `.env` is baked into the pushed image by `COPY backend/app/ .`.
- Binary WS frames use a 1-byte prefix (`1` = audio, `2` = JPEG). Audio is 16 kHz PCM in, 24 kHz PCM out. The prefixes are named in `main.py` (`AUDIO_PREFIX`/`JPEG_PREFIX`) and **shipped in the `config` frame**, so the client adopts them at runtime instead of keeping a second hardcoded copy that can drift. The literals in `useGeminiSocket.js` are only a fallback.
- The input rate is not assumed. The client reports what the browser actually granted via an `audio_config` message and the backend labels blobs with that; a browser that ignores the requested 16 kHz would otherwise send 48 kHz samples tagged as 16 kHz, which the model hears as gibberish with no error anywhere.
- **The digit signal has exactly one channel: the `{"type":"match"}` frame.** The backend also forwards the raw ADK event, which still contains the same `report_digit` call — acting on both made every detection fire `onDigitDetected` twice. The client logs `functionCall` and does nothing else with it.
- A session ends when *either* the client half or the model half finishes: `asyncio.wait(..., FIRST_COMPLETED)` over `upstream_task`/`downstream_task`, then cancel everything including the heartbeat. It used to be `gather(upstream, downstream, heartbeat)`, which never completed — `heartbeat_task` loops forever — so the `finally` never ran and the **billed Live session stayed open** after the client left. The heartbeat is deliberately not a lifecycle task; with `HEARTBEAT_ENABLED=false` it returns immediately and must not end the session.
- Cloud Run deploys pass `--timeout=3600`. A WebSocket is one long-lived request, so the default 300s request timeout was a hard five-minute cap on every Live session. `--min-instances=1` keeps the ADK/google-genai import cost off the first connection.
- The video capture loop in `useGeminiSocket.js` is a `setTimeout` chain, deliberately **not** `requestAnimationFrame`. rAF is throttled to zero in a hidden or backgrounded tab while the mic AudioWorklet keeps streaming, so tabbing away silently killed video and every detection with it, on a still-open billed session. Don't "optimize" it back.
- `context_window_compression` is enabled in `RunConfig`. Without it an audio+video session is capped at **2 minutes** (audio-only gets 15), which a single demo run can reach. ADK defaults it to `None`.
- The client clears its playback queue on `interrupted`, per the Live API guidance to stop playback on barge-in. `audioStreamer.stop()` posts `{action:'clear'}` and the worklet implements it as `readIndex = writeIndex`.
- **A new round must mint a new `session_id`.** `auto_create_session=True` creates a session when one is missing and **resumes** it when it isn't, so reconnecting with the same id continues the previous conversation. `BiometricLock.jsx` used to derive the id per *mount*, but the component stays mounted across rounds — so round 2 opened mid-scan with "Five digits." instead of "Scanner Online.", and round 3 answered once and went silent for the rest of the session. `startRound()` now mints a fresh UUID and passes the URL straight into `connect(url)`; reading it back from state would still hold the previous round's value in that tick. Verified against the real API: same id → no greeting, fresh id → "Scanner Online."
- `BiometricLock.jsx` fetches an audio clip from archive.org at runtime — it fails offline or under a restrictive CSP.
- **Continuous speech in the room silences the scanner outright** — zero tool calls, zero transcripts, downlink from ~83 kbit/s to 1.8. VAD reads a continuous voice as a user turn that never ends, so the model never takes one. Measured with `scripts/scan_accuracy.py --noise chatter` (0/5), and `--noise hiss` is the partial version (3/5). Vision is the robust half: blur, dim and backlit all scored 5/5, and only a near-black frame fails. If a live session feels broken while every component looks healthy, suspect the microphone before the camera.
- **`{"type":"ping"}` is echoed as `{"type":"pong"}`** without touching the model, so the client can separate network from thinking: the HUD's `Net` row is that round trip and `Detect − Net` is the model. Measured 1–2 ms locally. The *first* probe of a session reads high (~500 ms) because it lands while the Live socket is still being established; it settles from the next sample.
- **There is a client-side mic gate, and it is OFF by default** (`frontend/public/audio-processor.js`, enable with `?gate=1`). It exists because room noise scoring 0/5 is a *transport* problem — noise that never reaches the wire cannot hold a turn open — and because tuning the server's `AutomaticActivityDetection` instead only moved 0/5 to 1/5 (and `START_SENSITIVITY_LOW` risks dropping a softly-spoken user, so `realtime_input_config` stays on defaults). The floor tracks the **running minimum**: steady sound converges to it, intermittent speech rises above it; 250 ms pre-roll, 300 ms hangover (kept short: every extra ms is room noise the server's VAD hears instead of the silence that ends your turn), then ~400 ms of digital silence on close so the turn ends promptly instead of on a timeout. `node scripts/gate_check.mjs` verifies the decision rule offline against the real worklet, including that a room above the floor cap fails **open**. **Two separate implementations have now passed every offline check and then been reported broken on a real microphone** — the first held itself shut for a whole round, the second was "seems broken" after the fail-open rewrite. Synthetic signals cannot validate a gate; only a real mic in a real room can, and until someone does that with numbers this stays off. `?gate=1` enables it *and* streams the live `level` / `opens at` pair into the telemetry panel — reading those two numbers off the screen while speaking is the tuning pass; `GATE_OPEN_RATIO` and `GATE_FLOOR_MAX` are the dials. If it is ever turned on for real, the right architecture is `LiveRequestQueue.send_activity_start()/send_activity_end()` at the gate edges — otherwise the server's VAD never receives the silence that ends a turn and has to time out instead, which is felt as lag.
- **The telemetry panel's `MIC` row has three states, not two.** Every gate message is emitted from inside the gate, so with gating off — the default — nothing ever reported the mic as transmitting and the row sat on its initial `gated`: an ungated microphone pushing 256 kbit/s displayed as one sending nothing. It cost three separate "mic gated" reports before the row, rather than the gate, turned out to be the bug. `● open (ungated)` / `● transmitting` / `○ gated` now distinguish "no gate in play" from "gate currently shut".
- **`?hud=1` renders both telemetry panels with sample data and no socket**, so layout can be checked without a webcam or a billed session — which is how the panels' two overflow bugs were verified fixed rather than eyeballed. The samples are deliberately the awkward cases (long modality breakdown, four-digit tokens). The panels live in one right-hand column that owns the geometry, so they cannot disagree on width or grow into each other; a row's `detail` is its own line, because as a sibling of the value a long string widened the row past the panel edge.
- **`scripts/telemetry_view.py` renders a recorded session.** The live panels only hold the last 40 seconds, which cannot answer "why did that run feel bad?" — the question every debugging session here has started with. Expand the Trace panel, press **save**, and the browser downloads the whole run (per-second uplink split by video/microphone, downlink, both latencies, capture rate, token growth, and the event trace); the script renders it as one HTML timeline on a shared time axis, so a spike lines up against what was happening. Offline and free — it reads a file the browser wrote. Styling is imported from `telemetry_report.py` so the two outputs stay one design.
- `Telemetry.jsx` puts uplink/downlink kbit/s and two latencies on screen during a session, so a bad demo is diagnosable while it happens. `detect` is frame-sent → `match` frame; `speak` is that match → first model audio chunk. Neither is a socket round-trip and neither claims to be — the browser cannot measure one.
- `GEMINI.md` and `.gemini/skills/live/SKILL.md` hold the Live API reference (VAD, audio formats, ephemeral tokens). Read them before changing streaming behavior.
