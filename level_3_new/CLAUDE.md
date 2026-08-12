# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

Biometric Security System — a FastAPI + Google ADK backend streaming video/audio to the Gemini Live API, with a React/Vite frontend.

## Support scripts

Every `.sh` entrypoint starts with `#!/bin/bash`, `set -euo pipefail`, and `cd "$(dirname "${BASH_SOURCE[0]}")"`, so it operates on **this** directory no matter where it is invoked from. (They used to hardcode `cd $HOME/way-back-home/level_3_gemini` and act on the sibling copy.) Keep that preamble on any new script — the `cd` in particular, since an unguarded one used to let `build.sh` overwrite a `Dockerfile` in the caller's working directory.

Secrets are never passed on a command line or written world-readable: `~/gemini.key` and both generated `.env` files are `chmod 600`, and anything that prints `.env` pipes it through a `sed` redaction.

## Commands

- `make test` — `python -m pytest`. See "Tests can't fail" below.
- `make lint` — `ruff check .`, `ruff format --check .`, then `cd frontend && npm run lint`. Check-only; there is no `make fmt`. Run `ruff format .` to actually format.
- `make frontend` — `npm install && npm run build`. Required before the backend can serve the SPA; `main.py` mounts `frontend/dist` at `/` and only warns if it's missing. `frontend/node_modules` is absent on a fresh clone, so `make lint` fails until this runs.
- `make run` (8080, real API) · `make mock` (8080, offline fake server) · `make adk` (8000, ADK dev UI) · `make testadk` (interactive ADK CLI).
- `make verify` — `scripts/verify_setup.sh`; checks project, APIs, Python deps, and both `.env` files. Exits non-zero on failure, so it can gate a build.
- `make build` / `make deploy` delegate to `./build.sh` / `./deploy.sh` — one definition each, no duplicated gcloud invocation in the Makefile. Both derive `PROJECT_ID` from `~/project_id.txt` and default `SERVICE_NAME`/`REGION`/`IMAGE_PATH`; override via the shell. `make endpoint` shares the same `SERVICE_NAME`/`REGION` defaults.

Use `make mock` for frontend work — it replays `mock/mock_audio.pcm` and a canned tool call, so it costs nothing.

## Models

Never use 2.0 models (deprecated); 2.5 or later only. Production is `gemini-3.1-flash-live-preview`. `agent.py: get_model_id()` inspects `sys.argv` and falls back to `gemini-2.5-flash` under `adk run`, because the Live preview model 404s on `generateContent` — so `make testadk` exercises a different model than production. Live-model access is allowlisted; `./testmodels.sh` lists models supporting `bidiGenerateContent`.

## No monkey patching (removed)

`backend/app/patch_adk.py` is gone. ADK 2.6.3 handles Gemini 3.x Live natively — it detects the model via `_is_gemini_3_x_live()` and routes audio to `send_realtime_input(audio=)`, images to `video=`, and single-part text Content to `text=`. Do not reintroduce the patches; fix the caller instead.

The one thing this changed for callers: **`LiveRequestQueue.send_realtime()` accepts only `types.Blob`.** Text must go through `send_content()` — use the `send_text_stimulus()` helper in `main.py`. Passing a bare string raises a Pydantic `ValidationError` (the old patch hid this with `model_construct`).

## Environment

`GOOGLE_API_KEY`, `GEMINI_API_KEY`, and `GEMINI_KEY` must all be set to the same value — every config path sets all three. `GOOGLE_GENAI_USE_VERTEXAI=False` (Gemini API key path, not Vertex, despite the GCP setup). Secrets live outside the repo in `~/project_id.txt` and `~/gemini.key`; `runadk.sh` generates `backend/app/biometric_agent/.env` from them. There is no `.env.example`.

Installing deps: **always go through `./scripts/install_deps.sh`** (`--dev` also installs `requirements-dev.txt`). A bare `pip install -r requirements.txt` or `uv pip install -r requirements.txt` hard-fails with `ResolutionImpossible` / `No solution found`.

`requirements.txt` pins `websockets==17.0.1`, above the caps `google-adk` (`>=15.0.1,<16`) and `google-genai` (`>=13.0.0,<17`) declare. Those bounds are "last version we tested", not a real incompatibility — every websockets API these libraries call exists in 17.0.1 — so `overrides.txt` overrides them. `uv` applies it with `--override`; pip has no equivalent (`--constraint` can only tighten a bound), so the script installs the tree without websockets and then forces the pin with `--no-deps`. Both routes resolve to the same 56-package set.

Do **not** "fix" this with a bare `pip install -r requirements.txt --no-deps` — `--no-deps` applies to the whole command and skips every transitive dependency (pydantic, starlette, sqlalchemy…), leaving an app that can't import. `pip check` reporting the websockets conflict afterwards is expected.

`Dockerfile` passes `--override overrides.txt`, and `init.sh` calls the script, so `make build`, `cloudbuild.yaml`, and a fresh `./init.sh` all work. `init.sh` no longer runs `pip install google-adk --upgrade`: that ignored the `google-adk==2.6.3` pin and silently re-resolved `websockets` back below 16. If the Live socket misbehaves, drop the override and pin `websockets==15.0.1` to test whether it's the cause.

`main.py` hard-exits if `GOOGLE_API_KEY` is unset **only under `python main.py`** — the `sys.exit(1)` sits behind `if __name__ == "__main__"`. On import it logs two CRITICAL lines and continues (the comment promises it "raises error" otherwise; nothing does), which is what makes the offline test suite possible. `PORT` is hardcoded to 8080 and does **not** read Cloud Run's `$PORT`, despite the Dockerfile comment. `VIDEO_FPS` (default 2.0, clamped 0.5–5.0) is interpolated into the agent instruction, so backend and prompt stay in sync; `HEARTBEAT_INTERVAL` defaults to 10.0, clamped 5.0–30.0.

## Tests

`pytest.ini` sets `testpaths = tests backend/app/biometric_agent`, so `make test` collects only the hermetic suites: **15 tests, no API key, no network, no charge — and they can fail.** Report a green `make test` as verification of the protocol layer only.

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
- Binary WS frames use a 1-byte prefix (`1` = audio, `2` = JPEG). Audio is 16 kHz PCM in, 24 kHz PCM out.
- `BiometricLock.jsx` fetches an audio clip from archive.org at runtime — it fails offline or under a restrictive CSP.
- `GEMINI.md` and `.gemini/skills/live/SKILL.md` hold the Live API reference (VAD, audio formats, ephemeral tokens). Read them before changing streaming behavior.
