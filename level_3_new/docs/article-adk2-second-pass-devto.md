---
title: The Log Line That Never Ran — Auditing a Gemini Live Agent Against Its Own Docs
published: false
series: ADK
date: 2026-08-12 20:00:00 UTC
tags: python,googleadk,gemini,geminilive
canonical_url:
---

The ADK 2.x migration worked. Every test passed, the demo ran, the model answered. Then I read the docs next to the code and found a log line that had never executed, four code paths with no caller, and a video stream running at twice the documented maximum.

![Neon cyan wireframe hand in a HUD scanner reticle beside an audio waveform, with a terminal panel below whose log lines fade from cyan to dim grey around one conspicuously empty row](images/cover-adk2-second-pass-devto.jpg)

#### Third Article, Same Agent

This is the third pass over the same multimodal ADK agent — a browser streams webcam and microphone to Gemini 3.1 Flash Live, the model counts fingers and calls a tool.

1. [Building a Multimodal Agent with the ADK and Gemini Flash Live 3.1](https://medium.com/google-cloud/building-a-multimodal-agent-with-the-adk-and-gemini-flash-live-3-1-818c009977ac) — the original build, ADK 1.x plus a 187-line monkey patch.
2. The Monkey Patch Is Dead — the ADK 2.x migration, where that patch got deleted.
3. This one. The migration left things behind, and I only found them by treating the documentation as a diff against the source.

The migration article ended on a satisfied note: a major-version upgrade that touched only the compatibility layer. That was true and it was also incomplete. A framework upgrade tells you what stopped compiling. It says nothing about what still compiles and is wrong.

```console
git clone https://github.com/xbill9/way-back-home
cd way-back-home/level_3_new
```

#### The Short Version

Measured against `level_3`, the original design, still sitting in the same repo:

| | `level_3` | Now |
|---|---|---|
| `google-adk` | 1.27.2 | 2.6.3 |
| `requirements.txt` | 6 lines, 4 unpinned | 14 lines, fully pinned + `overrides.txt` |
| Media on the wire | base64 inside JSON | binary frames, 1-byte type prefix |
| Video capture loop | `setInterval` | `requestAnimationFrame` → back to a timer |
| Video frame rate | 2 FPS | 1 FPS, hard-clamped |
| Session length cap | 2 min (audio+video) | uncapped via context compression |
| Transcript logging | present, never ran | works |
| Barge-in | queued audio kept playing | playback queue cleared |
| Session creation | hand-rolled get-then-create | `auto_create_session=True` |
| WebSocket origin check | none | allowlist at handshake |
| Session teardown | `gather()`, never completed | `wait(FIRST_COMPLETED)` |

The rows that interest me are not the upgrades. They are the rows where the original was right and a later version made it worse.

#### Four Things With No Caller

Start with the easy category. Deleting code is the only refactor with no risk of a subtle behavior change, provided you have actually proven nothing calls it.

**The base64 JSON media path.** `main.py` had branches decoding `type: "audio"` and `type: "image"` payloads out of JSON. They were not speculative — they were `level_3`'s entire wire protocol:

```javascript
// level_3/frontend/src/useGeminiSocket.js
ws.current.send(JSON.stringify({
    type: 'image',
    data: base64,
    mimeType: 'image/jpeg'
}));
```

The rewrite moved media to binary frames with a 1-byte type prefix — `1` for audio, `2` for JPEG — which drops base64's 33% size penalty. The client was updated. The server branches stayed, and nothing had sent that shape since.

They came with two tests, which is the part worth pausing on. Those tests passed. They had always passed. A test that exercises a branch no caller reaches tells you the branch works; it tells you nothing about whether it should exist. When the code went, the tests went with it — they were pinning an implementation, not a contract.

**`proactivity` and `affective_dialog`.** Two query parameters on the WebSocket endpoint, documented in the docstring, read by nothing:

```python
async def websocket_endpoint(
    websocket: WebSocket,
    user_id: str,
    session_id: str,
    proactivity: bool = True,          # never read
    affective_dialog: bool = False,    # never read
) -> None:
```

In `level_3` these were real — they fed a conditional `RunConfig`. Then Gemini 3.1 Flash Live shipped without support for either, the config was removed, and the parameters outlived it. The docs are unambiguous, and I found the same sentence in three places: "these features are not yet supported in Gemini 3.1 Flash Live."

**A function-response scan that could never match.** Fifteen lines walking `server_content.model_turn.parts` looking for `function_response`, feeding a log loop below it. `model_turn` carries model output. A `functionResponse` is something a client sends. The list was structurally always empty.

**A second notification channel with no listener.** The frontend hook maintained `lastMessage` state, set it on match, system error and heavy metal, exported it — and no component ever read it. The callbacks drove the UI. This one is a repeat offender: the same codebase had already been bitten by a duplicate digit channel firing every detection twice.

Ninety lines out, one line in.

#### The Log Line That Never Ran

Now the interesting one.

```python
input_transcription = getattr(event, "input_audio_transcription", None)
if input_transcription and input_transcription.final_transcript:
    logger.info(f"USER TRANSCRIPT: {input_transcription.final_transcript}")
```

Two mistakes stacked. `input_audio_transcription` is the name of the `RunConfig` field that *enables* transcription — it is not the field on the event that transcription produces. And `final_transcript` is not a member of `types.Transcription` at all; the fields are `text`, `finished`, `language_code`, `speaker_label`, `words`.

Either mistake alone would have raised `AttributeError` and been fixed in minutes. Together, behind `getattr`'s default, they produce silence. The condition is `None and ...`, which is falsy, forever.

The evidence was sitting in a log I had already read. A 259-line server log from a real session, both transcriptions enabled in `RunConfig`, and not one `USER TRANSCRIPT:` or `GEMINI TRANSCRIPT:` line in it. I had scrolled past that twice. Absence is genuinely hard to notice — you cannot grep for a line that was never written, and nothing in the log says "by the way, transcription is configured and producing nothing."

It has never worked. The identical pattern is in `level_3`, at line 234, in the original design. Every version of this project has configured audio transcription, paid for audio transcription, and logged none of it.

The fix is two field names:

```python
input_transcription = getattr(event, "input_transcription", None)
if input_transcription and input_transcription.finished:
    logger.info(f"USER TRANSCRIPT: {input_transcription.text}")
```

Gating on `finished` is deliberate. ADK emits partial transcription events with `finished=False` and one accumulated event with `finished=True`, so this yields one clean line per turn rather than one per fragment. Its own `run_live()` docstring is the reference: partial *and* non-partial events are yielded to you, but only non-partial ones are saved to the session.

Verified live, which for this class of bug is the only verification that counts:

```
INFO - GEMINI TRANSCRIPT: Scanner Online.
```

That is the exact opening line the agent instruction specifies. First time this project has ever printed it.

#### The Optimization That Went Backwards

`level_3` captured video like this:

```javascript
intervalRef.current = setInterval(() => { /* capture, send */ }, 500);
```

The rewrite replaced it with `requestAnimationFrame` plus a manual elapsed-time check. On paper this is the better primitive: rAF is frame-aligned, it does not fire when the compositor has nothing to do, and pairing it with `toBlob` instead of `toDataURL` keeps the encode off the main thread.

In a backgrounded tab, rAF is throttled to zero.

The microphone is not. It runs in an AudioWorklet on the audio thread, which browsers keep alive precisely so playback and capture survive a tab switch. So the failure mode is asymmetric and silent: tab away, and video stops completely while audio streams on. The session stays open. The model keeps listening. Detection — the entire point of the application — stops, with no error on either side.

I watched it happen without recognizing it. Two sessions in one log:

```
Session 1: image frames #10, #20, #30, #40 — four detections
Session 2: 8,050 audio packets, zero image frames, zero detections
```

Session 2 was 65 seconds long. The difference between them was that I had switched to my terminal.

Timers are clamped in background tabs too — to roughly one second, not to zero. So the fix is to go back to what `level_3` did:

```javascript
const captureFrame = () => {
    if (ws.current?.readyState === WebSocket.OPEN) { /* capture, send */ }
    if (intervalRef.current !== null) {
        intervalRef.current = setTimeout(captureFrame, frameIntervalRef.current);
    }
};
```

Degrading to ~1 FPS beats stopping. And the self-rescheduling timeout re-reads the interval each tick, so the server's `config` frame stays authoritative — which the rAF version's manual elapsed-time check also did, and which a plain `setInterval` would not.

Two lessons here that generalize past this bug. The better primitive is only better under the conditions you tested it in; nobody tests a webcam demo with the tab hidden, because you are looking at the tab. And when you replace working code with something more sophisticated, the diff reviews as an improvement — the burden of proof quietly inverts.

#### Reading the Docs as a Diff Against Your Code

The remaining findings came from reading the Live API reference with the source open beside it. Two of them were numbers I had simply never checked.

**Video was running at twice the documented maximum.** From the capabilities guide:

> Video frames are sent as individual images (e.g., JPEG or PNG) at a specific frame rate (max 1 frame per second).

The project ran at 2 FPS and allowed up to 5 via an environment variable. Worse, the repo's own `GEMINI.md` stated "the recommended video frame rate is 2 per second" — and cited that exact page as its source. Somewhere between reading and writing, the number changed.

It worked. Nothing rejects the surplus frames. But you pay for them, and they consume the session budget twice as fast. `VIDEO_FPS` now defaults to 1.0 and is hard-clamped there — `VIDEO_FPS=3` yields 1.0 rather than being honoured. Documenting a contract you do not enforce is how the 2 FPS crept in.

**Audio+video sessions cap at two minutes.** From the session management guide:

> audio-only sessions are limited to 15 minutes, and audio-video sessions are limited to 2 minutes

Context window compression removes the cap entirely — sessions become unlimited. `RunConfig.context_window_compression` defaults to `None`, so it has to be asked for:

```python
context_window_compression=types.ContextWindowCompressionConfig(
    sliding_window=types.SlidingWindow(),
),
```

This application streams audio and video continuously, so it had been on the two-minute clock the whole time. My test sessions were 23 and 65 seconds, which is exactly why I had never seen it. A demo where someone fumbles through five gestures gets there comfortably.

**Interruption was documented and unhandled.** The guidance is explicit: on interruption, stop playing audio and clear queued playback. ADK surfaces `interrupted` on the event, and because the server forwards whole events as JSON, the flag was already arriving in the browser. Nothing read it. The clearing machinery already existed too — `audioStreamer.stop()` posts `{action:'clear'}`, which the worklet implements as `readIndex = writeIndex`. Three lines connected two things that were both already there.

#### What ADK 2.x Actually Asks Of You

The migration article covered the deletions. Here is the part that matters going forward, because 2.0 moved agents onto a graph engine and the new constraints fail quietly rather than loudly:

- **Use the plain `Agent`.** Custom `BaseNode` subclasses and `_run_async_impl()` / `generate_content()` overrides are *silently bypassed*. No error, no warning, just no effect.
- **Never hand-append events to the session.** It circumvents the graph engine and breaks determinism; `run_live()` owns the session.
- **Watch your exception handlers.** The framework now catches exceptions for retries and human-in-the-loop pausing. A broad `except` inside a node masks that; catching `BaseException` breaks pausing outright.
- **`run_live(session=...)` is deprecated.** Pass `user_id` and `session_id`.

This project passes all four, which is why the upgrade was uneventful — a plain `Agent`, `InMemorySessionService`, no hand-built events, and its broad handlers sitting in the transport layer rather than inside the graph.

One thing worth adopting: `Runner` gained `auto_create_session`. `run_live()` already calls `_get_or_create_session()` internally; without the flag, a missing session is a `ValueError`, which is why the code hand-rolled get-then-create. Setting it deletes the dance.

```python
runner = Runner(
    app_name=APP_NAME,
    agent=root_agent,
    session_service=session_service,
    auto_create_session=True,
)
```

And one worth skipping: `Runner(app=App(...))` is now described as the recommended construction, but `Runner(agent=..., app_name=...)` is explicitly still supported and gets wrapped into an `App` internally, with no deprecation warning. Churn without benefit. "Recommended" and "required" are different words.

You do not have to take my word on any of this:

```console
python -W error::DeprecationWarning -m pytest -q
```

Zero ADK warnings. That command is now the check after any ADK bump, which beats re-reading release notes.

#### Two Traps

**ADK's own docstring names a field that does not exist.** `run_live()` refers to `RunConfig.save_live_model_audio_to_session`. In 2.6.3 the real fields are `save_live_blob` and `save_live_audio`. When docs and installed source disagree, the source wins — it is the thing that runs.

**A green test suite proved less than it looked like.** The suites stub `runner.run_live()`, which is what makes them hermetic: no API key, no network, no charge. It also means session creation, resumption and teardown are never exercised. `make test` passes whether or not they work.

So `auto_create_session` and `context_window_compression` — the two changes most capable of breaking every session — were verified against the real API with a throwaway WebSocket client instead: connect with a fresh session id, confirm it auto-creates; reconnect on the same id, confirm it resumes; check the log for `SessionNotFoundError`. That caveat now lives in the project's own docs, because a green run there reads as more coverage than it is.

#### What Did Not Change

The wire protocol, the agent instruction, the tool definitions and the deployment path all came through untouched. Binary frames with a 1-byte prefix, 16 kHz PCM in and 24 kHz out, `report_digit` / `trigger_system_error` / `trigger_heavy_metal_mode` as server-side tools, Secret Manager for the API key, and the `get_model_id()` fallback to `gemini-2.5-flash` under `adk run`, since the Live preview model still 404s on `generateContent`.

#### What's Next

The next build targets an EAP Live model, and that is a separate project rather than another pass over this one — a different model tier changes enough about latency, modality handling and session behavior that pretending it is an upgrade to this codebase would be its own kind of dead code.

#### So What Really Changed?

- **Ninety lines deleted**, none of which had a caller: a superseded base64 path and its tests, two vestigial query parameters, a scan that could not match, and a notification channel with no listener.
- **Transcript logging works for the first time**, after silently logging nothing since the original design.
- **The capture loop went back to a timer**, because `requestAnimationFrame` stops dead in a background tab while the microphone does not.
- **Video dropped to 1 FPS and got a hard ceiling**, matching the documented maximum instead of doubling it.
- **Context compression is on**, lifting a two-minute cap the project had always been under.
- **Barge-in clears the playback queue**, connecting a flag that was arriving to machinery that already existed.
- **ADK creates the session**, and the 2.x constraints are written down where the next person will look.

#### Summary

Every one of these survived a framework migration, a test suite, a lint config and a demo that visibly worked. That is the through-line: none of them were the kind of failure a build catches. A dead branch compiles. A no-op log line runs. A throttled callback returns cleanly. An undocumented frame rate is accepted by the server.

If you have a Live agent of your own, three checks cost about an hour between them. Confirm your transcript handler has ever produced output — grep a real session log for a line you expect and cannot find. Confirm your video keeps flowing with the tab hidden. And read the session-limit page next to your `RunConfig`, because the two-minute audio+video cap is the kind of thing you discover during a demo rather than before one.

* * *

### Pre-publication checklist

- Technical claims were verified against `google-adk` 2.6.3 / `google-genai` 2.17.0 in `level_3_new` on 2026-08-12, with the `level_3` comparison taken from the same repo. Quotes from the Live API capabilities and session-management pages were taken that day — **re-confirm the 1 FPS and 2-minute figures before publishing**, since both are the kind of limit that moves.
- The transcript fix and the session-lifecycle checks were confirmed against the real API. **The interruption handler is wired but has not been observed firing** — it needs a human talking over the model — so either soften that claim or record a demo before publishing.
- The audio architecture of `gemini-3.1-flash-live-preview` (native audio vs half-cascade) is deliberately not asserted anywhere in this article; no published page states it, and the repo's own docs contradicted each other on the point. Do not add a claim about it during editing.
- Cover images are generated (`gemini-3.1-flash-lite-image`, one 16:9 source): `images/cover-adk2-second-pass-devto.jpg` at 1000×420 for dev.to, `images/cover-adk2-second-pass-v2.jpg` at 1376×768 for Medium. Upload to the dev.to cover slot rather than relying on the inline reference. The panel in the image carries invented glyph text (`SCAN_INIT`, `CORE_ACTIVE`) — it is decorative and matches nothing in the codebase, which is fine for a cover but do not caption it as real output.
- Link article 2 once it has a public URL — it is referenced by title in "Third Article, Same Agent" and currently has no href.
- Set `canonical_url` if cross-posting.
