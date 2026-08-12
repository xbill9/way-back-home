---
name: dev-run
description: Launch the Biometric Security System locally — picks between the mock server, the real Live API backend, and the ADK dev UI, and handles the frontend build step. Use when asked to run, start, serve, or manually try out this app.
---

# Running this app locally

Pick the entrypoint by what the user is actually working on. **Default to mock** unless they need real model behavior — the real backend bills every connection.

| Goal | Command | Port | Cost |
|---|---|---|---|
| Frontend / UI work | `make mock` | 8080 | free |
| Full stack, real model | `make run` | 8080 | billed |
| Poke the agent in a web UI | `make adk` | **8000** | billed |
| Poke the agent in a terminal | `make testadk` | — | billed, **different model** |
| Frontend hot reload | `cd frontend && npm run dev` | 5173 | proxies `/ws` → 8080 |

## Before anything

1. **The scripts cd into the wrong directory.** Every `.sh` entrypoint hardcodes `cd $HOME/way-back-home/level_3_gemini`, so `make run` / `make mock` / `make adk` / `make testadk` run the *sibling* copy. This is a known bug (see CLAUDE.md). Either fix the path in the script first, or run the underlying command directly from this directory:
   - `make run` → `cd backend && python app/main.py`
   - `make mock` → `python mock/mock_server.py`
   - `make adk` → `cd backend/app && adk web --host 0.0.0.0 --allow_origins 'regex:.*'`
   Say which one you did.
2. **Build the frontend** if `frontend/dist` is missing: `make frontend`. Without it the backend starts fine, prints a warning, and serves no UI — an easy misdiagnosis.
3. **For the real backend only:** `GOOGLE_API_KEY` must be exported or `main.py` hard-exits. `GEMINI_API_KEY` and `GEMINI_KEY` must be set to the same value. `make adk` also needs `backend/app/biometric_agent/.env`, which `runadk.sh` generates from `~/project_id.txt` and `$GOOGLE_API_KEY`.

## Running it

Start the server in the background so you keep control of the session, then wait for the port to accept connections before opening or curling anything. Report the URL (`http://127.0.0.1:8080/`) to the user.

If the user wants to see the UI, use the browser tooling to load it and screenshot — the app needs camera and mic permission to do anything interesting, so expect to describe what's on screen rather than drive a full session.

## Reading failures

- Connection opens then immediately closes → check the ADK/genai versions. `requirements.txt` pins `websockets==17.0.1`, deliberately above the caps `google-adk` (`<16`) and `google-genai` (`<17`) declare. If the Live socket misbehaves, drop to `websockets==15.0.1` to test whether the override is the cause.
- `ValidationError` on a text send → `LiveRequestQueue.send_realtime()` takes only `types.Blob`. Use `send_text_stimulus()` in `main.py`.
- 404 on the model → the Live preview model is allowlisted per-account. Run `./testmodels.sh` to list models with `bidiGenerateContent` support.
- `make testadk` behaving differently from `make run` → expected. `get_model_id()` falls back to `gemini-2.5-flash` under `adk run`.
- Blank page → `frontend/dist` wasn't built.
