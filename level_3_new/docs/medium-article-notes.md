# Notes: "Building a Multimodal Agent with the ADK and Gemini Flash Live 3.1"

Source: <https://medium.com/google-cloud/building-a-multimodal-agent-with-the-adk-and-gemini-flash-live-3-1-818c009977ac>
Author: xbill (William McLean) · Google Cloud – Community · 1 Apr 2026

Summary of the article's technical content, captured for offline reference. Not a full reproduction — see the link for the original text.

## What it covers

A walkthrough of building this project: an ADK agent (`biometric_agent`) on Gemini 3.1 Flash Live, fronted by a React/Vite UI, streaming audio and video bidirectionally over WebSocket, deployed to Cloud Run.

Section order:

1. Introduction and context
2. Python fundamentals and version management (pyenv)
3. Cloud Run overview
4. Gemini Live model capabilities
5. Gemini CLI tooling
6. Node version management (nvm)
7. ADK overview
8. Incremental development strategy
9. Environment setup
10. Frontend UI build
11. Mock server testing
12. ADK install verification
13. ADK web interface testing
14. Linting and tests
15. Local execution
16. Cloud Run deployment
17. Web interface operation
18. Key changes from the original codelab
19. Gemini CLI code review results
20. Summary and lessons learned

## Architecture

- **Backend** — Python ADK agent on Cloud Run, Gemini 3.1 Flash Live.
- **Frontend** — React/Vite, WebSocket bidirectional streaming, browser audio/video capture.
- **Wire protocol** — raw binary with a 1-byte type marker (`0x01` audio, `0x02` video) instead of JSON-wrapped payloads, to cut overhead.
- **Auth** — Gemini API key, *not* Vertex AI (`PROJECT_ID`/`REGION`).
- **Infra** — serverless Cloud Run, autoscaling.

## Configuration values quoted

| Parameter | Value |
|---|---|
| Python | 3.13.12 |
| Model (production) | `gemini-3.1-flash-live-preview` |
| Model (CLI fallback) | `gemini-2.5-flash` — avoids 404s |
| Video frame rate | 2 FPS |
| Heartbeat interval | 10.0 s |
| Video codec | JPEG, quality 0.6 |
| Audio / video markers | `0x01` / `0x02` |
| ADK web port | 8000 |
| App port | 8080 |
| Cloud Run region | us-central1 |

## Workflow commands

```
source init.sh          # environment init
source set_env.sh       # re-export after a session timeout
make frontend           # Vite build → dist/
make mock               # offline mock server, no API spend
make testadk            # ADK CLI; logs at /tmp/agents_log/agent.latest.log
make adk                # ADK web UI on :8000
make lint               # ruff check + format
make test               # pytest
make run                # local app on :8080
source deploy.sh        # Cloud Run deploy
make endpoint           # print service URL
npm install -g @google/gemini-cli
```

Cloud Shell needs the CORS override: `adk web --host 0.0.0.0 --allow_origins 'regex:.*'`.

## Components discussed

- `agent.py` — `Agent` with gesture-detection instructions; `get_model_id()` switches model by execution context.
- `patch_adk.py` — translation layer for Live API behavior ADK didn't handle natively; wraps `send_realtime_input` and manually unrolls `media_chunks`.
- `useGeminiSocket.js` — WebSocket client, binary encoding, AudioWorklet for off-main-thread audio, `toBlob` JPEG compression at 0.6.
- Keepalive — `CONTINUE_SURVEILLANCE` stimulus plus the "Neural handshake" opener, since the Live model isn't proactive.

## Issues and workarounds described

1. **Gemini API vs Vertex AI** — the original codelab used Vertex with project/region auth; Gemini 3.1 Live requires the Gemini API key path, and model availability differs between them.
2. **ADK gap for Gemini 3.1 Live** — `patch_adk.py` as a stopgap pending native support; GitHub issues filed upstream.
3. **Cloud Shell CORS** — explicit `--allow_origins` override.
4. **ADK CLI 404s** — context detection with fallback to `gemini-2.5-flash`.
5. **JSON wrapping removed** — replaced with raw binary streams for lower overhead.

## Recommendations given

- Add a graceful "Reconnecting…" UI state.
- Track ADK releases so the manual `media_chunks` unrolling can be dropped.
- Check that `.env` injection at deploy time keeps keys out of build logs.
- Move off the monkey patch once ADK supports this natively.

---

## Drift: where this repo no longer matches the article

Recorded 2026-08-11, after upgrading dependencies in `level_3_new`.

- **The monkey patch is gone.** The article's recommendation to "transition from monkey patch to native ADK support once available" has happened. `google-adk` 2.6.3 detects Gemini 3.x Live via `_is_gemini_3_x_live()` and routes `audio=` / `video=` / `text=` itself, so `backend/app/patch_adk.py` was deleted. See `GEMINI.md` for the caller-side consequence — `LiveRequestQueue.send_realtime()` now takes only `types.Blob`, so text goes through `send_content()`.
- **Test count differs.** The article cites 11 tests in 3.20 s; this tree collects 8, of which 7 run without an API key (`test_live_connection.py` aborts collection when no key is present).
- **`make lint` does not pass here.** 26 pre-existing ruff findings, exit 1 — ruff 0.16 applies its full rule set since the repo has no `ruff.toml`.
- **websockets pinned to 17.0.1**, deliberately above the `google-adk<16` / `google-genai<17` caps.
- **Python 3.13.14** locally, vs 3.13.12 in the article.
