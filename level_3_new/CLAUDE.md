# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

Biometric Security System — a FastAPI + Google ADK backend streaming video/audio to the Gemini Live API, with a React/Vite frontend.

## Known bug: scripts point at the wrong directory

Every `.sh` entrypoint hardcodes `cd $HOME/way-back-home/level_3_gemini`, so `make run`, `make mock`, `make adk`, `make testadk`, `./build.sh`, and `./frontend.sh` operate on the **sibling** directory instead of this one. This copy is meant to be self-contained — treat those paths as a bug and update them to `level_3_new` when working in this area. Only `make test` and `make lint` act on the current directory.

## Commands

- `make test` — `python -m pytest`. See "Tests can't fail" below.
- `make lint` — `ruff check .`, `ruff format --check .`, then `cd frontend && npm run lint`. Check-only; there is no `make fmt`. Run `ruff format .` to actually format.
- `make frontend` — `npm install && npm run build`. Required before the backend can serve the SPA; `main.py` mounts `frontend/dist` at `/` and only warns if it's missing. `frontend/node_modules` is absent on a fresh clone, so `make lint` fails until this runs.
- `make run` (8080, real API) · `make mock` (8080, offline fake server) · `make adk` (8000, ADK dev UI) · `make testadk` (interactive ADK CLI).
- `make deploy` / `make endpoint` read env vars from the **shell**, and they disagree: `deploy` uses `IMAGE_PATH`/`PROJECT_ID`/`GOOGLE_CLOUD_LOCATION`, `endpoint` uses `SERVICE_NAME`/`REGION`. Export them first or the command silently does the wrong thing.

Use `make mock` for frontend work — it replays `mock/mock_audio.pcm` and a canned tool call, so it costs nothing.

## Models

Never use 2.0 models (deprecated); 2.5 or later only. Production is `gemini-3.1-flash-live-preview`. `agent.py: get_model_id()` inspects `sys.argv` and falls back to `gemini-2.5-flash` under `adk run`, because the Live preview model 404s on `generateContent` — so `make testadk` exercises a different model than production. Live-model access is allowlisted; `./testmodels.sh` lists models supporting `bidiGenerateContent`.

## No monkey patching (removed)

`backend/app/patch_adk.py` is gone. ADK 2.6.3 handles Gemini 3.x Live natively — it detects the model via `_is_gemini_3_x_live()` and routes audio to `send_realtime_input(audio=)`, images to `video=`, and single-part text Content to `text=`. Do not reintroduce the patches; fix the caller instead.

The one thing this changed for callers: **`LiveRequestQueue.send_realtime()` accepts only `types.Blob`.** Text must go through `send_content()` — use the `send_text_stimulus()` helper in `main.py`. Passing a bare string raises a Pydantic `ValidationError` (the old patch hid this with `model_construct`).

## Environment

`GOOGLE_API_KEY`, `GEMINI_API_KEY`, and `GEMINI_KEY` must all be set to the same value — every config path sets all three. `GOOGLE_GENAI_USE_VERTEXAI=False` (Gemini API key path, not Vertex, despite the GCP setup). Secrets live outside the repo in `~/project_id.txt` and `~/gemini.key`; `runadk.sh` generates `backend/app/biometric_agent/.env` from them. There is no `.env.example`.

`main.py` hard-exits if `GOOGLE_API_KEY` is unset. `PORT` is hardcoded to 8080 and does **not** read Cloud Run's `$PORT`, despite the Dockerfile comment. `VIDEO_FPS` (default 2.0, clamped 0.5–5.0) is interpolated into the agent instruction, so backend and prompt stay in sync; `HEARTBEAT_INTERVAL` defaults to 10.0, clamped 5.0–30.0.

## Tests can't fail

Every root-level test wraps its body in `try/except Exception: print(...)`. `test_ws_backend*.py` need a server already on `ws://127.0.0.1:8080`, and `test_live_connection.py` makes a **real billed Live API call** against a `.env` that only exists after `runadk.sh`. These are accepted manual smoke checks — keep them as-is, but never report a green `make test` as verification.

Without that `.env`, `make test` doesn't even run: `test_live_connection.py` raises `ValueError: No API key was provided` at **collection**, which aborts the whole session. Use `python -m pytest --ignore=test_live_connection.py` (7 pass) to run the suite without a key or an API charge.

## Style

No `pyproject.toml` or ruff config anywhere up the tree, so ruff 0.16 applies its full built-in rule set (~413 rules — `I`, `BLE`, `ASYNC`, `SIM`, `RUF`, not just `E`/`F`). **`make lint` currently exits 1** with 26 pre-existing findings (blind `except Exception`, unsorted imports, an unused `# noqa: E402`); this predates any recent change, so don't treat it as a regression you introduced. Formatting is clean — `ruff format --check` passes. Adding a `ruff.toml` that pins a deliberate rule set would make the target meaningful.

The one non-default JS rule is `frontend/eslint.config.js`: `'no-unused-vars': ['error', { varsIgnorePattern: '^[A-Z_]' }]`. No TypeScript, no Prettier.

## Other gotchas

- `build.sh` regenerates `Dockerfile` from a heredoc every run — edits to `Dockerfile` are lost.
- Binary WS frames use a 1-byte prefix (`1` = audio, `2` = JPEG). Audio is 16 kHz PCM in, 24 kHz PCM out.
- `BiometricLock.jsx` fetches an audio clip from archive.org at runtime — it fails offline or under a restrictive CSP.
- `GEMINI.md` and `.gemini/skills/live/SKILL.md` hold the Live API reference (VAD, audio formats, ephemeral tokens). Read them before changing streaming behavior.
