# Do I Still Need a Monkey Patch for Gemini Live?

No. And deleting 187 lines of it was the single biggest benefit of moving to ADK 2.x — but it was not the only one, and it was not the last thing that needed fixing.

> **[FEATURED IMAGE — insert `docs/images/cover-adk2-second-pass-v2.jpg` (1376×768) here. Medium uses the first image in the story as the preview card, so it has to sit above the first heading. Delete this marker after inserting.]**

#### What Does This Agent Do?

The project is a biometric security scanner, built to exercise the parts of the Gemini Live API that a text chatbot never touches. A browser captures webcam and microphone, streams both to a FastAPI backend over a single WebSocket, and the backend forwards them to Gemini 3.1 Flash Live through the Agent Development Kit. The model watches the video feed, counts the fingers being held up, and calls a tool.

Three tools are registered:

- `report_digit(count)` — the detected finger count, which drives the UI
- `trigger_system_error()` — fired on an offensive gesture, which terminates the session
- `trigger_heavy_metal_mode()` — fired on the "Devil's Horns", a secret override

The transport is deliberately plain. Binary WebSocket frames carry a 1-byte type prefix — `1` for audio, `2` for JPEG — with 16 kHz PCM going up and 24 kHz PCM coming back, played through an AudioWorklet so the main thread stays free. Everything runs locally with `make run`, or on Cloud Run behind `make deploy`.

```console
git clone https://github.com/xbill9/way-back-home
cd way-back-home/level_3_new
```

Two versions live side by side in that repo: `level_3`, the original design, and `level_3_new`, the current one. Most of this article is the diff between them.

#### What Level 3 Needed to Work

The original build ran on `google-adk` 1.27.2, and it worked — but only because a file named `patch_adk.py` sat next to it, 187 lines long, applied at import time before anything else could run.

The problem it solved was real. Gemini 3.1 deprecated `media_chunks`, the field a 1.x ADK used to send realtime media. A 1.x ADK talking to a 3.1 Live model would send the deprecated shape and get nothing useful back. The patch monkey-patched three separate call sites to translate:

```python
# level_3_gemini/backend/app/patch_adk.py
if hasattr(rt_input, "media_chunks") and rt_input.media_chunks:
    logger.info("[PATCH] Unrolling 'media_chunks' from realtime_input.")
    for chunk in rt_input.media_chunks:
        ...
        await self.send_realtime_input(audio=chunk)
        ...
        await self.send_realtime_input(video=chunk)
```

The three targets were `live.AsyncSession.send_realtime_input`, which unrolled `media_chunks` into the new typed keywords; `GeminiLlmConnection.send_realtime`, which routed each blob to `audio=`, `video=` or `text=` by mime type; and `AudioCacheManager.cache_audio`, which was guarded against a `NoneType` blob that would otherwise raise.

It worked, and it was a liability. Monkey patching a framework means every upgrade is a gamble — the patch either becomes redundant, becomes wrong, or silently stops applying because the method it wraps was renamed. The closing recommendation in the original write-up was to delete it the moment the ADK supported the model natively.

#### What ADK 2.x Handles Natively

That moment arrived with `google-adk` 2.6.3, and `patch_adk.py` was deleted outright. The framework now does the routing itself, detecting the model generation and dispatching on it:

```python
# google/adk/models/gemini_llm_connection.py
if self._is_gemini_3_x_live or self._is_gemini_3_5_live_translate:
    if isinstance(input, types.Blob) and input.mime_type.startswith("audio"):
        await self._gemini_session.send_realtime_input(audio=input)
    else:
        await self._gemini_session.send_realtime_input(video=input)
else:
    await self._gemini_session.send_realtime_input(media=input)
```

Text is handled the same way. A single-part text `Content` is routed to `send_realtime_input(text=...)` for 3.x models rather than going out as client content, which matches the Live API's own guidance that `send_client_content` is only for seeding history.

That is the whole first patch target and the whole second one, upstream, maintained, and tested by someone else. The third — the `NoneType` guard on `cache_audio` — was not carried over, because upstream still calls `len(audio_blob.data)` unguarded. No path in this application produces a blob with `data=None`, so it stays deleted rather than being reintroduced as a precaution.

#### The One Thing That Broke

The patch was hiding a bug in the calling code, and deleting it exposed the bug rather than causing it.

`LiveRequestQueue.send_realtime()` accepts `types.Blob` and nothing else. The old patch had used `model_construct` internally, which skips Pydantic validation, so passing a bare string worked by accident. Without the patch it raises a `ValidationError`.

The call site that matters is the keepalive. This project sends a text stimulus every ten seconds when the client goes quiet, and under 1.x that stimulus was a string handed straight to `send_realtime()`. Text has to go through `send_content()` instead:

```python
def send_text_stimulus(live_request_queue: LiveRequestQueue, text: str) -> None:
    live_request_queue.send_content(
        types.Content(role="user", parts=[types.Part(text=text)])
    )
```

This is the removal most likely to take an agent off the air quietly. It does not fail at startup. It fails the first time the keepalive fires, ten seconds into a session that otherwise looks healthy. Anyone migrating a Live agent off 1.x should check that call site before touching anything else.

#### What Else Was Updated

The migration was the headline, but it was not the end of the work. Reading the Live API documentation with the source open beside it turned up several things that had been wrong the whole time, none of which any build, test or lint run had ever objected to.

**Video was running at twice the documented maximum.** The capabilities guide is specific:

> Video frames are sent as individual images (e.g., JPEG or PNG) at a specific frame rate (max 1 frame per second).

The project ran at 2 FPS and permitted up to 5 through an environment variable. Nothing rejects the surplus frames, which is why it went unnoticed — but they are billed, and they consume the session budget twice as fast. `VIDEO_FPS` now defaults to 1.0 and is hard-clamped there, so `VIDEO_FPS=3` yields 1.0 rather than being honoured. A documented limit that is not enforced is how the 2 FPS crept in to begin with.

**Audio-plus-video sessions cap at two minutes.** From the session management guide:

> audio-only sessions are limited to 15 minutes, and audio-video sessions are limited to 2 minutes

Context window compression removes the cap entirely. `RunConfig.context_window_compression` defaults to `None`, so it has to be asked for:

```python
context_window_compression=types.ContextWindowCompressionConfig(
    sliding_window=types.SlidingWindow(),
),
```

This application streams both continuously, so it had been on the two-minute clock since the first version. Short test sessions never reached it. A demo where someone works through five gestures does.

**Interruptions were documented and unhandled.** When a user talks over the model, the model stops generating — but the audio it already sent is sitting in the client's ring buffer and keeps playing. The Live API guidance is to stop playback and clear the queue on interruption. ADK surfaces `interrupted` on the event, and because the backend forwards whole events as JSON, the flag was already arriving in the browser with nothing reading it. The clearing machinery already existed too. Three lines connected them.

#### The Log Line That Never Ran

Both input and output audio transcription were enabled in `RunConfig` from the very first version of this project. Neither ever produced a line of output.

```python
input_transcription = getattr(event, "input_audio_transcription", None)
if input_transcription and input_transcription.final_transcript:
    logger.info(f"USER TRANSCRIPT: {input_transcription.final_transcript}")
```

Two mistakes are stacked here. `input_audio_transcription` is the `RunConfig` field that *enables* transcription — it is not the field on the event that transcription produces. And `final_transcript` is not a member of `types.Transcription` at all; the fields are `text`, `finished`, `language_code`, `speaker_label` and `words`.

Either mistake alone raises `AttributeError` and gets fixed in minutes. Together, behind the default on `getattr`, they produce silence. The condition evaluates to `None and ...`, which is falsy, forever.

The correct field names:

```python
input_transcription = getattr(event, "input_transcription", None)
if input_transcription and input_transcription.finished:
    logger.info(f"USER TRANSCRIPT: {input_transcription.text}")
```

Gating on `finished` is deliberate. ADK emits partial transcription events with `finished=False` and one accumulated event with `finished=True`, so this produces one clean line per turn instead of one per fragment. The `run_live()` docstring is the reference: partial and non-partial events are both yielded to the caller, but only non-partial ones are saved to the session.

The fix produces this on connect, which is the exact opening line the agent instruction specifies:

```
INFO - GEMINI TRANSCRIPT: Scanner Online.
```

#### Video That Stops When You Look Away

The original design captured frames on a timer:

```javascript
intervalRef.current = setInterval(() => { /* capture, send */ }, 500);
```

The rewrite replaced that with `requestAnimationFrame` and a manual elapsed-time check. On paper it is the better primitive — frame-aligned, idle when the compositor has nothing to do, and paired with `toBlob` instead of `toDataURL` it keeps JPEG encoding off the main thread.

In a backgrounded tab, `requestAnimationFrame` is throttled to zero.

The microphone is not. It runs in an AudioWorklet on the audio thread, which browsers keep alive so capture and playback survive a tab switch. The result is an asymmetric, silent failure: switch tabs and video stops completely while audio streams on. The WebSocket stays open, the session stays billed, and finger detection — the entire point of the application — stops with no error on either side. A 65-second session logged 8,050 audio packets and zero video frames.

Timers are throttled in background tabs as well, but to roughly one second rather than to zero, so the fix is a self-rescheduling timeout:

```javascript
const captureFrame = () => {
    if (ws.current?.readyState === WebSocket.OPEN) { /* capture, send */ }
    if (intervalRef.current !== null) {
        intervalRef.current = setTimeout(captureFrame, frameIntervalRef.current);
    }
};
```

Degrading to roughly 1 FPS beats stopping. Re-reading the interval on each tick also keeps the server's `config` frame authoritative, which the rAF version did and a plain `setInterval` would not.

#### Code With No Caller

Four things were deleted outright, for ninety lines removed and one added.

The **base64 JSON media path** decoded `type: "audio"` and `type: "image"` payloads out of JSON. It was not speculative — it was the original design's entire wire protocol, orphaned when media moved to binary frames with a type prefix. Its two tests went with it; they were pinning an implementation, not a contract anyone relied on.

The **`proactivity` and `affective_dialog` query parameters** were declared on the WebSocket endpoint, documented in the docstring, and read by nothing. In the original design they were real, feeding a conditional `RunConfig`. Gemini 3.1 Flash Live then shipped without support for either, the config was removed, and the parameters outlived it. The documentation is blunt: "these features are not yet supported in Gemini 3.1 Flash Live."

A **function-response scan** walked `server_content.model_turn.parts` looking for `function_response`. `model_turn` carries model output; a `functionResponse` is something a client sends. The list was structurally always empty.

A **second notification channel**, `lastMessage`, was set on every match, system error and heavy-metal trigger, exported from the frontend hook, and read by no component. The callbacks drive the UI.

#### Rules for Staying on ADK 2.x

ADK 2.0 moved agents onto a graph engine, and the new constraints fail quietly rather than loudly. Four are worth knowing before writing anything:

- **Use the plain `Agent`.** Custom `BaseNode` subclasses and `_run_async_impl()` or `generate_content()` overrides are *silently bypassed*. No error, no warning, no effect.
- **Never hand-append events to the session.** It circumvents the graph engine and breaks determinism; `run_live()` owns the session.
- **Watch broad exception handlers.** The framework catches exceptions for retries and human-in-the-loop pausing. A broad `except` inside a node masks that, and catching `BaseException` breaks pausing outright.
- **`run_live(session=...)` is deprecated.** Pass `user_id` and `session_id`.

This project satisfies all four, which is why the upgrade was uneventful: a plain `Agent`, `InMemorySessionService`, no hand-built events, and its broad handlers sitting in the transport layer rather than inside the graph.

One 2.x addition is worth adopting. `Runner` gained `auto_create_session`, and `run_live()` already calls its internal get-or-create helper — without the flag a missing session is a `ValueError`, which is why the code hand-rolled get-then-create:

```python
runner = Runner(
    app_name=APP_NAME,
    agent=root_agent,
    session_service=session_service,
    auto_create_session=True,
)
```

One is worth skipping. `Runner(app=App(...))` is now described as the recommended construction, but `Runner(agent=..., app_name=...)` is still supported and gets wrapped into an `App` internally, with no deprecation warning. "Recommended" and "required" are different words.

Whether any of this is still true after the next ADK release is checkable rather than a matter of reading changelogs:

```console
python -W error::DeprecationWarning -m pytest -q
```

Zero ADK warnings today. That command is the check after any bump.

#### Two Traps Worth Knowing

**ADK's own docstring names a field that does not exist.** `run_live()` refers to `RunConfig.save_live_model_audio_to_session`. In 2.6.3 the real fields are `save_live_blob` and `save_live_audio`. Where documentation and installed source disagree, the source wins — it is the thing that runs.

**A green test suite proves less than it looks like.** The suites stub `run_live()`, which is what makes them hermetic: no API key, no network, no charge. It also means session creation, resumption and teardown are never exercised, and the suite passes whether or not they work. The two changes most capable of breaking every session — `auto_create_session` and `context_window_compression` — were verified against the real API with a throwaway WebSocket client instead.

#### What Did Not Change

The wire protocol, the agent instruction, the tool definitions and the deployment path all came through the migration untouched: binary frames with a 1-byte prefix, 16 kHz PCM in and 24 kHz out, the three server-side tools, Secret Manager for the API key, and the model-id fallback to `gemini-2.5-flash` under `adk run`, since the Live preview model still 404s on `generateContent`.

A major-version framework upgrade that touches only the compatibility layer is the outcome to want. It is also the argument for keeping shims isolated in one file with a name that says what it is.

#### What's Next

The next build targets an EAP Live model, as a separate project rather than another pass over this one. A different model tier changes enough about latency, modality handling and session behaviour that treating it as an upgrade to this codebase would create exactly the kind of compatibility layer this article is about deleting.

#### So What Really Changed?

- **`patch_adk.py` is deleted** — 187 lines and three monkey-patched call sites, replaced by native routing in ADK 2.6.3.
- **Text goes through `send_content()`**, because `send_realtime()` takes `types.Blob` only. The keepalive is the call site that catches people out.
- **Video runs at 1 FPS with a hard ceiling**, matching the documented maximum instead of doubling it.
- **Context compression is enabled**, lifting a two-minute cap on audio-plus-video sessions.
- **Transcript logging works**, after reading a `RunConfig` field name off the event since the original design.
- **The capture loop is a timer again**, because `requestAnimationFrame` stops dead in a background tab while the microphone does not.
- **Barge-in clears the playback queue**, connecting a flag that was already arriving to machinery that already existed.
- **Ninety lines with no caller are gone**, along with the two tests that covered them.

#### Summary

The Agent Development Kit 2.x release removed the need for a compatibility patch that had been carried since the original build, and deleting it was almost entirely subtractive — the pleasant kind of upgrade. The one removal that bites is `send_realtime()` rejecting anything that is not a `types.Blob`, which surfaces as a keepalive failure ten seconds into an otherwise healthy session rather than as a crash at startup.

The wider lesson came after the migration. A framework upgrade tells you what stopped compiling; it says nothing about what still compiles and is wrong. A dead branch compiles. A no-op log line runs. A throttled callback returns cleanly. An undocumented frame rate is accepted by the server. Every one of these survived a migration, a test suite, a lint config and a demo that visibly worked, and each was found by reading the documentation next to the code rather than by running anything.

For anyone running a Live agent of their own, three checks cost about an hour between them: confirm the transcript handler has ever produced output, confirm video keeps flowing with the tab hidden, and read the session-limit page next to your `RunConfig`.

* * *

### Medium formatting notes

Medium's editor does not parse Markdown — pasted `####` and backticks stay literal, and repairing that by hand is an edit pass you should not have to do. Medium *does* accept rich text, so paste from the rendered page instead:

`docs/article-adk2-second-pass-medium.html` — open it in a browser, or use the hosted copy at https://claude.ai/code/artifact/3cea5d52-9f63-4103-a81d-f3a91be60afa

1. Click into the article, **Ctrl/Cmd+A**, **Ctrl/Cmd+C**. The instructions panel, the image placeholder and the checklist are `user-select: none`, so select-all takes the article and nothing else.
2. Paste into an empty Medium draft. Headings, code blocks, block quotes, links, bold and inline code all survive. The first line becomes the title and the second the subtitle.
3. Drag `docs/images/cover-adk2-second-pass-v2.jpg` in at the top, above the first heading, so it becomes the preview card image.
4. Spot-check the two block quotes — the 1 FPS and 2-minute limits are the article's load-bearing claims.
5. Submit to the Google Cloud - Community publication, and set the canonical URL if dev.to publishes first.
6. Tags: ADK, Gemini, Python, Google Cloud, AI Agents.

This file and the dev.to version share one source; the only differences are the front matter, the image marker, and these notes. Medium has no table support, so keep comparisons as lists if you edit.
