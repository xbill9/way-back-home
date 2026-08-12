---
name: deploy-cloudrun
description: Build and deploy the Biometric Security System to Cloud Run as the biometric-scout service.
disable-model-invocation: true
---

# Deploying to Cloud Run

Target service: `biometric-scout`, image `gcr.io/${PROJECT_ID}/biometric-scout`, `--allow-unauthenticated`.

$ARGUMENTS

## Confirm before you start

Deployment is outward-facing and billed. Confirm with the user which project and region you're deploying to before running anything, and echo back the resolved values.

## The env-var trap

The Makefile targets read from the **shell**, and they disagree with each other:

- `make deploy` needs `IMAGE_PATH`, `PROJECT_ID`, `GOOGLE_CLOUD_LOCATION`, `GOOGLE_API_KEY`
- `make endpoint` needs `SERVICE_NAME`, `REGION`

An unset variable expands to empty and `gcloud` fails or targets the wrong thing rather than erroring usefully. Print each value before invoking. `GOOGLE_API_KEY`, `GEMINI_API_KEY`, and `GEMINI_KEY` all get the same value; `GOOGLE_GENAI_USE_VERTEXAI=False`.

## Two paths

**Manual (`build.sh` + `make deploy`).** Note that `build.sh` **regenerates `Dockerfile` from a heredoc** before building — any hand edits to `Dockerfile` are silently overwritten. If the user has customized it, stop and flag this. `build.sh` also hardcodes `cd $HOME/way-back-home/level_3_gemini` (see CLAUDE.md), so it may build the sibling directory; run `gcloud builds submit . --tag gcr.io/${PROJECT_ID}/biometric-scout` from here instead.

**Cloud Build (preferred, single step).**
```
gcloud builds submit --config cloudbuild.yaml --substitutions=_GOOGLE_API_KEY=YOUR_KEY
```
Defaults: `_SERVICE_NAME=biometric-scout`, `_REGION=us-central1`, timeout 800s. Never paste the real key into a message or a committed file.

## Verify

1. `make endpoint` (with `SERVICE_NAME`/`REGION` exported) to get the URL.
2. Fetch `/` and confirm the SPA is served — if the build stage didn't produce `frontend/dist`, the container starts healthy but serves no UI.
3. Open a WebSocket to `/ws/{user_id}/{session_id}` to confirm the Live connection works, not just that the container is up.

`main.py` hardcodes port 8080 and ignores Cloud Run's `$PORT`. This works only because 8080 is the default — if a revision is configured with a different container port, it will fail to become ready.
