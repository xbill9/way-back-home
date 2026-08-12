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

## Where the values come from

Nothing needs to be exported for a default deploy.

**`PROJECT_ID` cannot be overridden from the shell.** `build.sh:12` and `deploy.sh:16` both do `PROJECT_ID=$(<"$PROJECT_ID_FILE")` unconditionally, so an exported `PROJECT_ID` is silently ignored and the deploy targets whatever `~/project_id.txt` names. This is a billed, `--allow-unauthenticated` deploy — **read that file and echo its contents to the user before running anything**, and never confirm a project the user named without checking it against the file. To actually retarget, edit `~/project_id.txt`.

The rest genuinely are `${VAR:-default}` and do respond to the shell: `SERVICE_NAME=biometric-scout`, `REGION=us-central1`, `IMAGE_PATH=gcr.io/${PROJECT_ID}/${SERVICE_NAME}`, `SECRET_NAME=gemini-api-key`. `make endpoint` shares the `SERVICE_NAME`/`REGION` defaults.

## The API key never travels on a command line

`deploy.sh` syncs `~/gemini.key` into Secret Manager (creating the secret and adding a version only when the value actually changed), grants the Cloud Run runtime SA `roles/secretmanager.secretAccessor`, and wires it in with `--set-secrets`, mapping `GOOGLE_API_KEY`, `GEMINI_API_KEY`, and `GEMINI_KEY` to the same secret. **Never reintroduce the key as a `--set-env-vars` value or a build substitution** — both are readable after the fact, from the revision spec and from retained build history.

`--set-env-vars` replaces the entire environment, so all four plain variables go in one comma-separated flag. Repeating the flag keeps only the last occurrence.

## Both paths are currently broken at the image build

`Dockerfile:30` runs `uv pip install --no-cache-dir --system -r requirements.txt`, which **cannot resolve**: `google-adk==2.6.3` requires `websockets>=15.0.1,<16` while `requirements.txt` deliberately pins `websockets==17.0.1`. `uv` reports *"No solution found … your requirements are unsatisfiable"*; pip reports `ResolutionImpossible`. This affects `make build` and `cloudbuild.yaml` equally, since both build the same `Dockerfile`.

Fix the `Dockerfile` before deploying — install everything except the pin, then force it:

```dockerfile
RUN grep -v '^websockets' requirements.txt > /tmp/req.txt && \
    uv pip install --no-cache-dir --system -r /tmp/req.txt && \
    uv pip install --no-cache-dir --system --no-deps websockets==17.0.1
```

Flag this to the user rather than silently editing the `Dockerfile` — it is checked in and is the source of truth.

## Two paths

**Manual (`make build` + `make deploy`).** `build.sh` runs `gcloud builds submit . --tag "${IMAGE_PATH}"`. `Dockerfile` is checked in and is the source of truth — the script used to regenerate it from a heredoc and silently discard hand edits, but no longer does.

**Cloud Build (single step).**
```
gcloud builds submit --config cloudbuild.yaml
```
Defaults: `_SERVICE_NAME=biometric-scout`, `_REGION=us-central1`, `_SECRET_NAME=gemini-api-key`, timeout 800s. It builds, pushes, and deploys with the same `--set-secrets` wiring. `deploy.sh` must have run at least once to create the secret and the IAM binding.

## Verify

1. `make endpoint` to get the URL (same `SERVICE_NAME`/`REGION` defaults as the deploy).
2. Fetch `/` and confirm the SPA is served — if the build stage didn't produce `frontend/dist`, the container starts healthy but serves no UI.
3. Open a WebSocket to `/ws/{user_id}/{session_id}` to confirm the Live connection works, not just that the container is up.

`main.py` hardcodes port 8080 and ignores Cloud Run's `$PORT`. This works only because 8080 is the default — if a revision is configured with a different container port, it will fail to become ready.
