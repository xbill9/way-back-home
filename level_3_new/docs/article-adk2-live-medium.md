# The Monkey Patch Is Dead — Updating a Gemini Live Agent for ADK 2.x

A follow-up on the same multimodal ADK agent, four months later: what the Agent Development Kit 2.x release train changed for Gemini Live, which workarounds can now be deleted, and the one removal that will quietly take your agent off the air.

> **[FEATURED IMAGE — insert `docs/images/cover-adk2-live-v2.jpg` (1376×768) here. Medium uses the first image in the story as the preview card, so it has to sit above the first heading. Delete this marker after inserting.]**

#### Didn't You Already Write This Article?

I did. In April I published a walkthrough of building a real-time multimodal agent on the ADK with Gemini 3.1 Flash Live, deployed to Cloud Run:

[Building a Multimodal Agent with the ADK and Gemini Flash Live 3.1](https://medium.com/google-cloud/building-a-multimodal-agent-with-the-adk-and-gemini-flash-live-3-1-818c009977ac)

The honest caveat in that piece was a file named `patch_adk.py` — 187 lines of monkey patching that taught a 1.x ADK how to talk to a 3.1 Live model. The closing recommendation was to drop it the moment the ADK supported the model natively.

That moment arrived. This article is the migration: what shipped in ADK 2.x, what got deleted, and what broke on the way.

The updated code lives in a new directory in the same repo, so both versions stay side by side:

```console
git clone https://github.com/xbill9/way-back-home
cd way-back-home/level_3_new
```

#### The Short Version

Six things changed between the April article and today:

- **`google-adk`** — was 1.x plus a monkey patch, now 2.6.3 with native support
- **`google-genai`** — was 2.14.0, now 2.17.0
- **`patch_adk.py`** — was required at import time, now deleted outright
- **Text into the model** — went through a patched `send_realtime()`, now goes through `send_content()`
- **The deployed API key** — was a plaintext `--set-env-vars`, now Secret Manager via `--set-secrets`
- **`requirements.txt`** — was unpinned, now fully pinned

Everything below is the detail behind those six lines.

#### What Landed in ADK 2.x

ADK 2.0.0 went GA in May. The headline features were the multi-agent workflow engine and native inter-agent routing — neither of which is why this project changed. For anyone doing bidirectional streaming, the interesting material is in the point releases. Per the upstream changelog:

- **2.2.0** — Gemini 3.1 Flash Live protocol support, `turn_complete_reason` carrying safety information, session reconnection improvements
- **2.3.0** — Gemini Live 3.1 input transcription, translation config in `RunConfig`
- **2.4.0** — streamed thought, media and code-execution deltas, plus `response_scheduling` to control Live function-response behavior
- **2.5.0** — voice activity detection events surfaced from Live sessions, non-blocking tool execution as background tasks
- **2.6.0** — state deltas for live mode via `LiveRequest`

Which is to say: everything the patch was faking is now first-party, and a good deal more on top of it.

#### Read the Installed Source, Not the Release Notes

Release notes tell you a feature exists. They do not tell you whether it covers your specific call path. Before deleting anything, I went looking in the installed package for the exact behavior the patch was providing.

The patch existed because ADK sent legacy `media=` blobs to a model that wanted `audio=`, `video=` and `text=`. In `google/adk/models/gemini_llm_connection.py`:

```python
if self._is_gemini_3_x_live or self._is_gemini_3_5_live_translate:
    if input.mime_type and input.mime_type.startswith('audio/'):
        await self._gemini_session.send_realtime_input(audio=input)
    elif input.mime_type and input.mime_type.startswith('image/'):
        await self._gemini_session.send_realtime_input(video=input)
    ...
else:
    await self._gemini_session.send_realtime_input(media=input)
```

That is the patch, upstream, verbatim in spirit. The model detection is worth confirming rather than assuming, because it is a string match against your model ID:

```console
>>> from google.adk.utils import model_name_utils as m
>>> m._is_gemini_3_x_live('gemini-3.1-flash-live-preview')
True
```

Text is handled as well — ADK's `_send_content` routes a single-part text `Content` to `send_realtime_input(text=...)` for 3.x models, which was the third thing the patch did.

Three of the four patches were genuinely obsolete. Delete and move on.

Except for the fourth.

#### The One That Bites

`patch_adk.py` also patched `LiveRequestQueue.send_realtime` to build its `LiveRequest` with Pydantic's `model_construct`, skipping validation. I had filed that one mentally as defensive. It was load-bearing.

Native ADK types the parameter strictly:

```python
def send_realtime(self, blob: types.Blob) -> None:
    self._queue.put_nowait(LiveRequest(blob=blob))
```

The backend was handing it **plain strings** in three places: the initial "Neural handshake" that wakes the model, inbound user text, and the `CONTINUE_SURVEILLANCE` heartbeat that fires every ten seconds when the video feed goes quiet. The old patch swallowed all three. Native ADK raises `ValidationError`.

The failure mode is nasty precisely because it is partial. Remove the patch without touching those call sites and the agent still connects, still streams video, still detects gestures — then goes mute after the first turn, because the keepalive for a non-proactive model is now throwing inside a task nobody is watching.

The fix is to stop pretending text is realtime media. Text is content:

```python
def send_text_stimulus(live_request_queue: LiveRequestQueue, text: str) -> None:
    """Send a text turn to the model.

    LiveRequestQueue.send_realtime() only accepts types.Blob; text has to go
    through send_content(). For Gemini 3.x Live, ADK routes a single-part text
    Content to send_realtime_input(text=...) internally.
    """
    live_request_queue.send_content(
        types.Content(role="user", parts=[types.Part(text=text)])
    )
```

Three call sites changed to route through it:

```python
-    live_request_queue.send_realtime("Neural handshake")
+    send_text_stimulus(live_request_queue, "Neural handshake")

-    live_request_queue.send_realtime(user_text)
+    send_text_stimulus(live_request_queue, user_text)

-    live_request_queue.send_realtime("CONTINUE_SURVEILLANCE")
+    send_text_stimulus(live_request_queue, "CONTINUE_SURVEILLANCE")
```

Same bytes on the wire, through an API that is actually supported. **Grep your own code before you upgrade** — `grep -n "send_realtime(" -r .` and look at what you are passing. If any of it is a `str`, that is your outage.

One patch I deliberately did not carry over: the null guard on `AudioCacheManager.cache_audio`. Upstream still calls `len(audio_blob.data)` with no check, so a blob with `data=None` raises. Nothing in this app produces one — but if you were seeing that error under load, keep your guard.

With the call sites fixed, the import-time patching disappears from both entry points:

```python
-# Patch ADK for Gemini 3.1 Live API compatibility
-import patch_adk
-
-patch_adk.apply_patches()
```

#### Pinning the Dependency Matrix

An unpinned `requirements.txt` is how the original project drifted into needing a patch at all — `init.sh` ran `pip install google-adk --upgrade` unconditionally on every setup, so no two environments were the same. Everything is now pinned:

```console
google-adk==2.6.3
google-genai==2.17.0
fastapi==0.141.1
uvicorn==0.52.1
websockets==17.0.1
python-dotenv==1.2.2
anyio==4.14.2
```

One of those pins is a deliberate constraint violation. `google-adk` requires `websockets<16` and `google-genai` requires `websockets<17`; current websockets is 17.0.1, so a naive upgrade-everything fails resolution outright.

I went over both caps on purpose, after checking what the libraries actually touch. `google/genai/live.py` uses exactly four symbols from websockets — `ConnectionClosed`, `asyncio.client.connect`, `asyncio.client.ClientConnection`, and `frames.CLOSE_CODE_EXPLANATIONS`. All four exist unchanged in 17.0.1, and both uvicorn WebSocket implementations import cleanly against it. The caps read as conservative rather than load-bearing.

```console
pip install -r requirements.txt --no-deps
```

`pip check` will now report two violated constraints, permanently. That is a real cost — this is a combination nobody upstream tests. Pin it explicitly, comment the reason in the file, and make "drop back to 15.0.1" the first diagnostic step if Live sessions start behaving strangely.

#### Proving the Transport Works Without Paying For It

The awkward part of Live API work is that real verification costs money and needs a valid key, which makes it tempting to ship on "it imports fine."

There is a cheaper test that answers most of the question. Start the server with a deliberately invalid key and read the error:

```plaintext
2026-08-11 - ERROR - APIError in live flow: 1007 None. API key not valid.
```

`1007` is a WebSocket protocol-level close code. Receiving it means the socket opened, the handshake completed, frames were exchanged, and Google's endpoint rejected the credentials — the entire transport path works and only auth failed. That one line validated the websockets 17 override end to end for free. A genuine transport incompatibility would have failed earlier, and differently.

It is not a full verification. It says nothing about how a session behaves over ten minutes of streaming. But it cleanly separates "my stack is broken" from "my key is wrong," which is most of the debugging value at none of the cost.

#### Verify the Environment

A new `make verify` target gates everything else — project, enabled APIs, Python dependencies, and both generated `.env` files. It exits non-zero, so it can front a build:

```console
xbill@penguin:~/way-back-home/level_3_new$ make verify
./scripts/verify_setup.sh
🚀 Verifying Mission Alpha (Level 3) Infrastructure...

✅ Google Cloud Project: aisprint-491218
✅ Cloud APIs: Active
✅ Python Environment: Ready
❌ Env Configuration: Missing .env backend/app/biometric_agent/.env
   Run: ./init.sh (root .env) and ./runadk.sh (agent .env)

-------------------------------------------------------
🛑 SYSTEM CHECKS FAILED. Please resolve the issues above.
make: *** [Makefile:29: verify] Error 1
```

That is the check doing its job on a clean tree: the agent `.env` is generated by `runadk.sh` from `~/project_id.txt` and `~/gemini.key`, and both generated files are written `chmod 600`.

#### Test the Interface Without Spending Tokens

The mock server still replays a canned audio buffer and a fake tool call, which remains the right way to do frontend work:

```console
xbill@penguin:~/way-back-home/level_3_new$ make mock
http://127.0.0.1:8080/
INFO:     Started server process [530494]
INFO:     Waiting for application startup.
INFO:     Application startup complete.
INFO:     Uvicorn running on http://0.0.0.0:8080 (Press CTRL+C to quit)
```

> **[IMAGE — insert the mock-server UI screenshot here. The April article's version is still hosted on Medium's CDN at `1*ggxtOqokR95T3jPqVT_tLw.png`; retake it if the HUD has changed. Delete this marker after inserting.]**

#### Lint and Test — With an Honest Caveat

This is where I have to contradict the April article, which showed a clean `make lint` and eleven green tests. Here is the same tree today:

```console
xbill@penguin:~/way-back-home/level_3_new$ make lint
ruff check .
...
BLE001 Do not catch blind exception: `Exception`
  --> test_ws_backend_v2.py:70:12
   |
68 |             await listener_task
69 |
70 |     except Exception as e:
   |            ^^^^^^^^^
71 |         print(f"Error: {e}")
   |

Found 26 errors.
[*] 9 fixable with the `--fix` option (2 hidden fixes can be enabled with the `--unsafe-fixes` option).
make: *** [Makefile:24: lint] Error 1
```

Those 26 findings are not a migration regression — I checked by running the identical command against an untouched copy, which produced 29. The project has no `ruff.toml` anywhere up the tree, so ruff 0.16 applies its full built-in rule set: import sorting, blind-except, async and simplification rules, not just the `E`/`F` defaults the earlier output implied. Formatting is clean; `ruff format --check` passes. Adding a `ruff.toml` that pins a deliberate rule set is what would make this target mean something again.

The tests deserve a sharper warning:

```console
xbill@penguin:~/way-back-home/level_3_new$ python -m pytest --ignore=test_live_connection.py
============================= test session starts ==============================
platform linux -- Python 3.13.14, pytest-9.1.1, pluggy-1.6.0
rootdir: /home/xbill/way-back-home/level_3_new
plugins: asyncio-1.4.0, anyio-4.14.2, cov-7.1.0
collected 7 items

backend/app/biometric_agent/test_agent.py .....                          [ 71%]
test_ws_backend.py .                                                     [ 85%]
test_ws_backend_v2.py .                                                  [100%]
======================== 7 passed, 4 warnings in 1.04s =========================
```

Every root-level test wraps its body in `try/except Exception: print(...)`, so a failure prints and passes. Two of them need a server already listening on `ws://127.0.0.1:8080`, and `test_live_connection.py` makes a real, billed Live API call — which is also why it has to be excluded to run the suite at all: with no key present it raises at *collection* and aborts the whole session.

They are useful manual smoke checks. They are not a green build, and I should not have presented them as one the first time around. If you copied this test layout from the April article, that is worth an hour of your time.

#### One Definition Per Deployment

The original `Makefile` carried its own inline `gcloud run deploy`, separate from `deploy.sh`, which meant two definitions drifting apart. There is now one:

```console
build:
	./build.sh

deploy:
	./deploy.sh
```

Two things in that deploy path are worth stealing regardless of what you are building.

**The API key comes from Secret Manager, not the environment.** `deploy.sh` creates or updates a `gemini-api-key` secret from `~/gemini.key`, grants the runtime service account `secretAccessor`, and wires it in at deploy time. A key passed as `--set-env-vars` is readable afterwards by anyone who can describe the service:

```console
  --set-secrets="GOOGLE_API_KEY=${SECRET_NAME}:latest,GEMINI_API_KEY=${SECRET_NAME}:latest,GEMINI_KEY=${SECRET_NAME}:latest"
```

**`--set-env-vars` replaces the entire environment.** Repeat the flag and only the last occurrence survives. The old Makefile passed seven of them and therefore deployed exactly one variable — `MODEL_ID` — with the others silently discarded. Every variable now goes in a single comma-separated flag:

```console
  --set-env-vars="GOOGLE_CLOUD_PROJECT=${PROJECT_ID},GOOGLE_CLOUD_LOCATION=${REGION},GOOGLE_GENAI_USE_VERTEXAI=False,MODEL_ID=gemini-3.1-flash-live-preview"
```

#### Hardening the Support Scripts

Less glamorous, equally worth doing. Every `.sh` entrypoint now opens with `set -euo pipefail` and `cd "$(dirname "${BASH_SOURCE[0]}")"`, so it acts on its own directory no matter where it was invoked. Previously several of them hardcoded a `cd` into the *sibling* lab directory, and an unguarded one let `build.sh` overwrite a `Dockerfile` in whatever directory you happened to be standing in.

Two related fixes:

- `Dockerfile` is now checked in and authoritative. `build.sh` used to regenerate it from a heredoc on every run, quietly discarding any edit you had made.
- `.gcloudignore` excludes `.env` and `*.key` explicitly. This one is easy to get wrong: once a `.gcloudignore` exists it takes full precedence over `.gitignore` for build uploads, so the `.gitignore` rule that protects your key locally does nothing for the image. Without the explicit entries, the agent `.env` that `runadk.sh` generates gets baked straight into the pushed container by `COPY backend/app/ .`.

#### What Did Not Change

Worth stating plainly, because it is the good news. The wire protocol, the frontend, the agent instructions and the tool definitions all came through the ADK 2.x migration untouched:

- Binary WebSocket frames with a 1-byte type prefix — `1` for audio, `2` for JPEG
- 16 kHz PCM in, 24 kHz PCM out, AudioWorklet on the client
- 2 FPS video, JPEG quality 0.6
- `report_digit`, `trigger_system_error` and `trigger_heavy_metal_mode` as server-side tools
- The `get_model_id()` fallback to `gemini-2.5-flash` under `adk run`, since the Live preview model still 404s on `generateContent`

A major-version framework upgrade that touches only the compatibility layer is the outcome you want. It is also the argument for keeping shims isolated in one file with a name that tells you what it is.

#### So What Really Changed?

- **`patch_adk.py` is deleted.** ADK 2.6.3 detects Gemini 3.x Live and routes `audio=` / `video=` / `text=` natively.
- **Text no longer goes through `send_realtime()`.** It takes `types.Blob` only; text goes through `send_content()`, and the keepalive is the call site that will catch you out.
- **Dependencies are pinned**, including a documented, deliberate `websockets` override past two upstream caps.
- **Secrets moved to Secret Manager**, and all env vars into one `--set-env-vars` flag.
- **`make verify` gates setup**; `make build` and `make deploy` have one definition each.
- **Scripts are self-locating and secrets are `chmod 600`** — no more acting on the caller's working directory.
- **The test suite is documented as the smoke check it actually is**, rather than reported as a passing build.

#### Summary

The migration off a compatibility patch was almost entirely subtractive, which is the pleasant kind of upgrade. The lesson that generalizes: when you delete a workaround, check every patch against the current upstream source individually — and be most suspicious of the ones that look merely defensive. Three of mine were obsolete. The fourth was hiding an API misuse in my own code, and it would have shipped as a silent partial outage rather than a crash.

If you are running a Gemini Live agent on ADK 1.x with shims of your own, the specific thing to check before you touch anything is `LiveRequestQueue.send_realtime()`. It takes `types.Blob`, only. If your keepalive is a string, fix that first.

* * *

### Staging notes — Medium

*Delete this entire section before publishing.*

**Differences from the dev.to version** (`article-adk2-live-devto.md`), which stays the canonical source:

- **No YAML front matter.** Medium takes the first `#` heading as the story title and the following paragraph as the subtitle/kicker. Both are set above.
- **No tables.** The "Short Version" comparison table is a six-item bulleted list here. Medium has no table support in the editor and silently drops them on paste.
- **No inline images.** The two image references are replaced with bracketed insertion markers. Medium will not resolve relative paths — images must be dragged into the editor, which uploads them to Medium's CDN.

**Publishing steps:**

1. Paste the body into a new Medium draft. Verify code blocks survived as code blocks; if they came through as plain paragraphs, retype the opening triple-backtick on each — Medium converts it in place.
2. Inline code spans (single backticks) do **not** convert on paste. Either accept the literal backticks or fix the high-traffic ones manually — `patch_adk.py`, `send_realtime()`, `send_content()`, `types.Blob`.
3. Drop `docs/images/cover-adk2-live-v2.jpg` at the top, above the first heading, so it becomes the preview card image.
4. Drop the mock-server screenshot at its marker, then delete both markers.
5. Submit to the Google Cloud - Community publication, and set the canonical URL if dev.to publishes first.
6. Tags: ADK, Gemini, Python, Google Cloud, AI Agents.

**Verify before publishing:** every technical claim was checked against `google-adk` 2.6.3 / `google-genai` 2.17.0 in `level_3_new` on 2026-08-12. The **ADK 2.x changelog bullets and the 2.0.0 GA date come from upstream release notes, not hands-on testing** — re-confirm, especially if ADK has moved since.
