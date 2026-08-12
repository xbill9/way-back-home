# The Monkey Patch Is Dead: Updating a Gemini Live Agent for ADK 2.x

*A follow-up to [Building a Multimodal Agent with the ADK and Gemini Flash Live 3.1](https://medium.com/google-cloud/building-a-multimodal-agent-with-the-adk-and-gemini-flash-live-3-1-818c009977ac). If you built along with that piece, this is what has changed underneath you — and the one migration that will bite.*

When I wrote that article in April, the honest caveat was buried in the middle: getting Gemini 3.1 Flash Live to work with the Agent Development Kit required a file called `patch_adk.py`. It monkey-patched `google.genai` and three ADK classes at import time. I ended the piece recommending you "transition from monkey patch to native ADK support once available."

That has now happened. I deleted `patch_adk.py` this week and the agent works better without it. Here is what changed, and what breaks when you remove it.

## The short version

| | April | Now |
|---|---|---|
| google-adk | 1.x + monkey patch | 2.6.3, native |
| google-genai | 2.14.0 | 2.17.0 |
| `patch_adk.py` | 187 lines, required | deleted |
| Text input path | patched `send_realtime()` | `send_content()` |

## What ADK 2.x brought

ADK 2.0.0 went GA on May 19, 2026. The headline features were the multi-agent workflow engine — non-linear, conditional and cyclical execution graphs — and native inter-agent routing with control handoffs and context propagation. Neither of those is why this project changed.

The interesting arc for anyone doing bidirectional streaming is in the point releases. Per the changelog:

- **2.2.0** — Gemini 3.1 Flash Live protocol support, `turn_complete_reason` carrying safety information, session reconnection improvements
- **2.3.0** — Gemini Live 3.1 input transcription, translation config in `RunConfig`
- **2.4.0** — streamed thought/media/code-execution deltas, `response_scheduling` to control Live function-response behavior
- **2.5.0** — voice activity detection events surfaced from Live sessions, non-blocking tool execution as background tasks
- **2.6.0** — state deltas for live mode via `LiveRequest`

In other words, everything `patch_adk.py` was faking is now first-party, and a good deal more besides.

## Reading the source instead of the release notes

The patch existed because ADK sent legacy `media=` blobs to a model that expected `audio=`, `video=` and `text=`. Before deleting anything I checked what ADK 2.6.3 actually does now. In `google/adk/models/gemini_llm_connection.py`:

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

That is the patch, upstream. The model detection is worth confirming rather than assuming:

```python
>>> from google.adk.utils import model_name_utils as m
>>> m._is_gemini_3_x_live('gemini-3.1-flash-live-preview')
True
```

Text is handled too. ADK's `_send_content` routes a single-part text `Content` to `send_realtime_input(text=...)` for 3.x models — the third thing the patch did.

So three of the four patches were genuinely obsolete. Delete, and move on.

Except.

## The one that bites

`patch_adk.py` also patched `LiveRequestQueue.send_realtime` to use Pydantic's `model_construct`, bypassing validation. I had assumed that was defensive. It was load-bearing.

Native ADK types the parameter strictly:

```python
def send_realtime(self, blob: types.Blob) -> None:
    self._queue.put_nowait(LiveRequest(blob=blob))
```

My backend was passing **plain strings** to it in three places — the initial "Neural handshake" that wakes the model, user text messages, and the `CONTINUE_SURVEILLANCE` heartbeat that fires every ten seconds when the video feed goes quiet. The old patch silently accepted them. Native ADK raises `ValidationError`.

If you remove the patch without touching those call sites, your agent still connects, still streams video, still detects gestures — and then goes mute after the first turn, because the heartbeat that keeps a non-proactive model engaged is now throwing inside a task you probably aren't watching.

The fix is to use the supported path. Text is not realtime media; it is content:

```python
def send_text_stimulus(live_request_queue: LiveRequestQueue, text: str) -> None:
    """Text goes through send_content(); send_realtime() takes only types.Blob."""
    live_request_queue.send_content(
        types.Content(role="user", parts=[types.Part(text=text)])
    )
```

For a Gemini 3.x Live model, ADK turns that back into `send_realtime_input(text=...)` internally — the same wire behavior the patch produced, through an API that is actually supported.

**Check your own code for this before upgrading.** `grep -n "send_realtime(" ` and look at what you are passing. If any of it is a `str`, that is your outage.

One patch I did not carry over: the guard on `AudioCacheManager.cache_audio`. Upstream still calls `len(audio_blob.data)` without a null check, so a blob with `data=None` will raise. No path in my app produces one, but if you were seeing that error under load, keep the guard.

## Dependency pins, and one deliberate violation

While I was in there I pinned everything, because an unpinned `requirements.txt` was how the original project drifted into needing a patch in the first place. `init.sh` ran `pip install google-adk --upgrade` unconditionally on every setup.

Two constraints are worth knowing about. `google-adk` requires `websockets<16`; `google-genai` requires `websockets<17`. Current websockets is 17.0.1, so a naive "upgrade everything" fails resolution outright.

I went over both caps on purpose, after checking what the libraries actually touch. `google/genai/live.py` uses exactly four things from websockets: `ConnectionClosed`, `asyncio.client.connect`, `asyncio.client.ClientConnection`, and `frames.CLOSE_CODE_EXPLANATIONS`. All four exist unchanged in 17.0.1, and both uvicorn WebSocket implementations import cleanly against it. The caps look conservative rather than load-bearing.

```
pip install --no-deps websockets==17.0.1
```

`pip check` will now report two violated constraints, permanently. That is a real cost — you are running a combination nobody tests. Pin it explicitly, comment why, and be ready to drop back to 15.0.1 as the first diagnostic step if Live sessions start misbehaving.

## Proving it works without paying for it

The awkward part of Live API work is that a real verification costs money and needs a valid key, so it is tempting to skip it and ship on "it imports fine."

There is a better cheap test. Start the server with a deliberately invalid key and watch what comes back:

```
2026-08-11 - ERROR - APIError in live flow: 1007 None. API key not valid.
```

`1007` is a WebSocket protocol-level close code. Getting it means the socket opened, the handshake completed, frames were exchanged, and Google's endpoint rejected the credentials — i.e. the entire transport path works, and only auth failed. That single line validated the websockets 17 override end to end for free. A transport incompatibility would have failed earlier and differently.

It is not a full verification — it says nothing about whether a live session behaves correctly over minutes. But it cleanly separates "my stack is broken" from "my key is wrong," which is most of the debugging value at a fraction of the cost.

## Using Claude Code for the migration

The April article used Gemini CLI as the development environment. I ran this migration through Claude Code, and the useful difference was less about code generation than about verification discipline.

The pattern that worked:

**Write down the traps first.** `/init` generates a `CLAUDE.md` that gets loaded into every session. Mine records the things that are not visible in the code — that a green `make test` is meaningless because the tests swallow exceptions, that one test makes a billed API call, that `websockets` is deliberately pinned past its caps. Context that would otherwise be re-derived, or worse, not.

**Make the agent check rather than assume.** The single most valuable instruction was to verify claims against the installed source. That is how the `send_realtime()` string problem surfaced before it shipped rather than after: reading `live_request_queue.py`, then actually calling it with a string to confirm `ValidationError`, instead of reasoning that removing a patch labeled "compatibility" should be safe.

**Test upgrades in a throwaway venv.** The whole dependency matrix — including the websockets override and whether `patch_adk` still applied — was resolved in a scratch virtualenv before anything touched the ambient interpreter.

**Establish a baseline before believing a failure.** After the migration `make lint` exited 1 with 26 findings and `make test` aborted during collection. Both looked like regressions. Running the identical commands against an untouched copy of the project showed 29 findings and the same collection abort — pre-existing, caused by a missing `.env` and by ruff 0.16 applying its full rule set to a project with no `ruff.toml`. Without that comparison I would have spent an hour fixing the wrong thing.

Skills and hooks handle the rest: a `/dev-run` skill that knows which of four entry points to launch and which ports they bind, and a `PostToolUse` hook running `ruff format` on every edited Python file.

## What to take away

If you are running a Gemini Live agent on ADK 1.x with compatibility shims, the upgrade is worth doing and mostly subtractive. Read your patches, check each one against current upstream source, delete what is now native — and pay close attention to the ones that look defensive, because those are the ones hiding an API misuse in your own code.

Watch `LiveRequestQueue.send_realtime()` specifically. It takes `types.Blob`, only. If your keepalive is a string, fix that before you delete anything.

---

### Publishing notes

Same draft works for both venues with small swaps:

- **dev.to / AWS Builders** — the deployment section is Cloud Run here. Either cut the deploy references entirely and keep it platform-neutral (the ADK/Live content is portable), or swap in the Lightsail flow: `save-aws-creds.sh`, `make deploy-lightsail`, `make lightsail-status`. Add tags: `ai`, `python`, `googlecloud`, `webdev`.
- **Medium / Google Cloud Community** — runs as-is. Link it as a direct follow-up to the April piece in the first line, which the current intro already does.
- **Both** — the "Proving it works without paying for it" and "Using Claude Code for the migration" sections are the original contributions; if you need to cut for length, cut the changelog bullets instead, since those are available upstream.

**Verify before publishing:** every technical claim here was checked against `google-adk` 2.6.3 / `google-genai` 2.17.0 on 2026-08-11 in `level_3_new`. The changelog bullets under "What ADK 2.x brought" come from the upstream release notes, not from hands-on testing — worth a second look if ADK has moved since. The ADK 2.0.0 GA date (May 19, 2026) and its two headline features came from a partially-loaded GitHub release page; confirm both before publishing.
