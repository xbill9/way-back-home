# Testing strategy

## The problem this solves

Before this, nothing in the repo could gate a regression. `make test` either
aborted at collection (`test_live_connection.py` raises `ValueError: No API key
was provided` at import) or ran three root-level scripts whose bodies are each
wrapped in `try/except Exception: print(...)`. A green run meant "Python
executed", not "the system works".

The core discovery that unlocks real testing: **`backend/app/main.py` imports
cleanly with no API key and no network.** The missing-key `sys.exit(1)` is
guarded by `if __name__ == "__main__"`, and the only call that reaches Gemini is
`runner.run_live()` inside `downstream_task`. Stub that one call and the entire
WebSocket endpoint — config frame, binary demux, JSON dispatch, dedup, teardown —
is drivable in-process for free.

## The tiers

| Tier | What it covers | Cost | Runs in `make test` |
|---|---|---|---|
| 1. Unit | Agent tools, `get_model_id`, pure helpers | free | yes |
| 2. Protocol | The WebSocket endpoint via `TestClient` + stubbed runner | free | yes |
| 3. Contract | The JS/Python frame-prefix agreement | free | yes |
| 4. Frontend | PCM conversion, packet framing, socket state | free | not yet — needs vitest |
| 5. Live | Real billed Live API call | billed | never; opt-in only |

Tiers 1–3 are the gate. Tiers 4–5 are opt-in.

## Tier 1 — Unit (exists, 18 tests)

`backend/app/biometric_agent/test_agent.py` already asserts properly on
`report_digit`, `trigger_system_error`, `trigger_heavy_metal_mode`, and both
`get_model_id` branches. These are real tests — CLAUDE.md's "tests can't fail"
warning applies to the **root-level** files only.

`test_live_models.py` adds the Live audio EAP rules: the feature matrix
(thinking on clever-chatter only, BLOCKING function calls refused there), model
id normalisation, the `adk run` fallback for every Live-only model, and the two
ADK workarounds — `build_live_model()` returning an instance rather than a
string, and `EapLiveGemini` forcing the Gemini 3.x Live protocol flag. It uses
`mock.patch.dict` / `mock.patch.object`, so it unwinds on failure.

One weakness worth fixing: both `get_model_id` tests in `test_agent.py` mutate
`os.environ` and `sys.argv` directly and restore by hand, so a failure mid-test
leaks state into whatever runs next. Convert to `monkeypatch.setenv` /
`monkeypatch.setattr`, which unwinds on failure.

## Tier 2 — Protocol (built, 20 tests)

`tests/conftest.py` + `tests/test_ws_protocol.py`.

Two fixtures do the work:

- **`spy`** replaces `LiveRequestQueue` with a recorder and stubs
  `runner.run_live` with an async generator that yields nothing **and stays
  open**. It keeps `send_realtime` and `send_content` in separate lists, which
  is what lets a test assert text never leaked into `send_realtime` — the exact
  regression the removed `patch_adk.py` used to mask.
- **`ws_connect`** opens the real endpoint over Starlette's in-process transport,
  optionally with request headers (used by the origin-policy tests).

**Why the stub awaits forever rather than returning.** An immediate `return`
means "the Live API hung up", and since the shutdown fix the endpoint reacts to
that correctly by tearing the session down — before the client's frames are ever
read. The faithful stub is one that stays open between turns, like the real
generator. Getting this wrong turns ten protocol tests red at once, which is the
useful failure mode.

**A trap this fixture used to contain**, worth keeping written down: it wrapped
teardown in `contextlib.suppress(Exception)` to hide the broken shutdown. If you
ever reintroduce a suppression here, it must wrap only the `__exit__` call.
Writing it as

```python
with contextlib.suppress(Exception), client.websocket_connect(path) as ws:
    yield ws
```

puts the `yield` inside the `suppress`, and `@contextmanager` re-raises the
caller's exception *at* that yield — so `suppress` eats it, `__exit__` returns
True, and **every assertion inside a `with ws_connect()` block passes
vacuously**. That is the exact anti-pattern this whole document exists to remove,
reintroduced in the fixture meant to remove it. The suppression is gone now that
teardown is clean. If you touch this fixture, re-run the check regardless: put
`assert False` inside a `with ws_connect()` block and confirm the test fails.

A related rule for tests here: **assert on unique strings.** `main.py:227` sends
`"Neural handshake"` unconditionally at every connect, so a test asserting that
phrase reached the queue passes even if the entire client-text branch is deleted.
The first draft of `test_text_never_reaches_send_realtime` had exactly that bug.

Covered in `test_ws_protocol.py` (frame by frame): config frame ordering and
contents, prefix `1` → `audio/pcm;rate=16000`, prefix `2` → `image/jpeg`,
header-only frames dropped, unknown prefixes ignored, malformed JSON not fatal,
queue closed on disconnect.

The two base64-JSON tests that used to sit here are gone with the code they
covered. `main.py` had `type: "audio"` / `type: "image"` branches decoding
base64 out of JSON, but the wire contract is binary frames with a 1-byte prefix
and nothing had sent the JSON shape in a long time. The tests existed to cover
the branch, not to pin a contract any client relied on.

Covered in `test_ws_session.py` (whole connection): the config frame carries the
binary prefixes, no synthetic model turn precedes the model, the client-reported
sample rate labels the blobs (and a bogus one is ignored), origin allow/deny/open
policy, `trigger_system_error` does not write to the socket it closed, and the
session ends on client disconnect.

All of these detect change rather than just exercising lines, and each fix was
checked against a mutation that reverts it:

| Mutation | Test that goes red |
|---|---|
| input rate 16000 → 24000 | `test_prefix_1_routes_to_16khz_audio` |
| `return` → `break` in the system-error path | `test_system_error_does_not_write_to_the_closed_socket` |
| `check_origin` always returns True | `test_disallowed_origin_is_refused` |

## Tier 3 — Contract (done, the better way)

The frame protocol used to be written down twice and shared nowhere — `packet[0]
= 1` / `= 2` in `useGeminiSocket.js` against `if msg_type == 1` / `== 2` in
`main.py`. Either end could change without the other noticing, and the failure
was silent: audio arrives tagged as JPEG and the model just behaves badly.

Rather than the regex-the-JS test suggested here originally, the second option
was taken: `main.py` names `AUDIO_PREFIX`/`JPEG_PREFIX` and ships them in the
existing `config` frame, and the client adopts them at runtime. The literals in
`useGeminiSocket.js` are now only a fallback for a server too old to send them,
so there is one definition and it lives on the server.
`test_config_frame_carries_the_binary_prefixes` asserts the frame carries them.

The same pattern fixed the input sample rate, which was the other silently-shared
assumption: the client now reports the rate the browser actually granted via an
`audio_config` message rather than both ends assuming 16 kHz.

## Tier 4 — Frontend (nothing exists)

`frontend/package.json` has no test runner. The high-value targets are pure
`ArrayBuffer` math and need no browser:

- `audioRecorder.js` — Float32 → Int16 PCM conversion, including clipping at
  ±1.0 and the sample-rate contract (16 kHz in).
- `audioStreamer.js` — 24 kHz output buffering and queue drain.
- `useGeminiSocket.js` — the `Uint8Array(len + 1)` packet framing.

Add `vitest` + `jsdom`, wire `npm test`, and add it to `make test`. The eslint
config already exists, so tooling conventions are settled.

## Tier 5 — Live (opt-in, billed)

`test_live_connection.py` stays a manual smoke check. It no longer prints its
error and passes anyway — the `try/except` is gone and it is marked
`@pytest.mark.live`, because it is now the fastest way to answer "does this key
reach `models/walkie-talkie`", and a check that passes on refusal cannot answer
that. It honours `MODEL_ID`, so the other EAP endpoint is one env var away.

One thing left to fix: `genai.Client(...)` is still constructed at import, which
is why collection used to abort. Move it inside the test function.

Run it on purpose: `python -m pytest test_live_connection.py -s`.

It is also the only tier that can validate the EAP work at all. Tiers 1–3 check
that the right config is *assembled*; whether `models/walkie-talkie` accepts it —
`SILENT` scheduling, the forced Gemini 3.x realtime routing, audio-only
modality — is unknowable offline.

`test_ws_backend.py` / `test_ws_backend_v2.py` are superseded by Tier 2 for
protocol coverage. Keep them as manual end-to-end probes against a running
server, or delete them once Tier 2 covers the same ground against the real model.

## Tier 6 — Behaviour (built, billed, non-interactive)

`scripts/scan_accuracy.py` is the only tier that measures whether the **model**
does its job, as opposed to whether the plumbing around it is correct. Tiers 1–3
stub `run_live()` and therefore pass regardless of what the model does; Tier 5
proves a socket opens. Neither can tell you the scanner stopped recognising
hands.

    ./scripts/scan_accuracy.py                                  # 5 digits, sharp
    ./scripts/scan_accuracy.py --rounds 2 --blur-prob 0.6 --jitter 6
    ./scripts/scan_accuracy.py --min-rate 0.8                   # gate a release

It drives the real WebSocket endpoint with no human: fixture JPEGs stand in for
the webcam, a `{"type":"text"}` frame stands in for saying "scan", and every
`report_digit` is scored against the known count. One billed Live session per
run, about a minute.

### Making the fixtures — the part worth reusing

There were no hand images, and hand-labelling a webcam session is exactly the
manual step this tier exists to remove. The fixtures were **generated** with
`gemini-3.1-flash-lite-image` (Interactions API, see the `nb2lite-image` skill),
one prompt per finger count, then downscaled to 640x480 JPEG q60 — bit-for-bit
the format `useGeminiSocket.js` puts on the wire, so the backend cannot tell them
from a browser.

Two rules make this trustworthy rather than circular:

1. **Verify the ground truth by eye before committing.** Image models are
   notoriously bad at hands; a prompt saying "exactly three fingers" is not
   evidence the image has three. Every fixture here was looked at first. A
   mislabelled fixture doesn't fail loudly, it makes the harness confidently
   wrong.
2. **Generate the input, never the expectation.** The model under test is asked
   to count; the count it is scored against comes from a human eye, not from
   another model. Score with a model and you are measuring agreement between two
   models, which can be high and wrong together.

### What it cannot tell you

Fixtures are sharp, centred, well lit and static. That is the easy case, so a
green run is a weaker claim than a green run by hand — it proves the pipeline and
the prompt, not that a moving hand in bad light is legible. `--blur-prob` and
`--jitter` approximate a real capture; they are not one. **It sends no audio at
all**, so anything involving VAD, barge-in, or a microphone picking up the
speakers is outside its reach — which is precisely the gap that left the
2026-08-13 session unexplained.

### The condition sweep

`--matrix` runs nine conditions, one Live session each (~7 minutes, billed).
Results from 2026-08-13 at 1 FPS, five trials per condition:

| condition | hits | p50 | notes |
|---|---|---|---|
| baseline | 5/5 | 0.95s | |
| motion blur (60%) | 5/5 | 0.65s | |
| dim light | 5/5 | 0.68s | luma 55; countable by eye |
| very dark | **0/5** | -- | all five "inadequate lighting." |
| backlit | 5/5 | 0.71s | |
| overexposed | 5/5 | 1.70s | slowest visual condition |
| room hiss | **3/5** | 0.65s | 1 wrong, 1 refused; speech lagged a trial |
| background chatter | **0/5** | -- | five silences, no reply at all |
| worst case | **0/5** | -- | same total silence |

**Vision is not the fragile part. Audio is.** Every visual degradation short of
near-black passed; the model is markedly better at counting fingers in a blurred,
dim or backlit frame than the "Stabilize hand." refusal rate in real sessions
suggests. "Very dark" failing is arguably correct behaviour rather than a bug --
the fixture is at luma 23 and the model says so precisely ("inadequate lighting",
the exact phrase instruction rule 2 offers it).

**Continuous speech in the room stops the scanner dead.** Not degraded -- silent:
zero tool calls, zero transcripts, and the downlink collapses from ~83 kbit/s to
1.8. The mechanism is VAD: a continuous voice means the user's turn never ends,
so the model never takes one. Room hiss is the partial version of the same thing
(3/5, with the *speech* running a full trial behind the tool call while the calls
stayed correct).

This is the strongest available explanation for a session that "works" in every
component and still feels broken to the person using it, and it is invisible to
every other tier — they send no audio.

### Two findings from building it, both counter-intuitive

- **Ask too soon and you get the previous hand.** The model answers ~1s after
  the stimulus using video it has *already ingested*. With `--stimulus-delay 0.5`
  every trial reported its predecessor's digit — 0/5, all off by one turn. At 4s
  it was 5/5. This is the closest thing yet to an explanation for a session that
  feels broken while every component works.
- **More frames did not help.** 1.0 and 2.0 FPS both scored 10/10 under 60% blur;
  1.0 answered faster (0.68s vs 1.80s median). The frame rate had been the prime
  suspect for a bad live session on reasoning alone, and the measurement
  contradicted it. That is the entire point of having this tier.

## What `make test` does now

`pytest.ini` sets `testpaths = tests backend/app/biometric_agent`, so default
collection picks up only the hermetic suites: **57 tests, no API key, no network,
no charge, and they can fail.** The root-level smoke scripts are untouched and
run by explicit path.

What they deliberately do **not** cover: `runner.run_live()` is stubbed, so
session creation, resumption and teardown are never exercised. `make test` is
green whether or not those work — the session lifecycle and `RunConfig` have to
be verified against the real API with a WebSocket client.

Markers `live` and `needs_server` are registered for the opt-in tiers.

## Blockers found while building this

Three things surfaced that are worth fixing on their own merits:

1. ~~**The endpoint never shuts down cleanly.**~~ **Fixed.** It was
   `await asyncio.gather(upstream_task(), downstream_task(), heartbeat_task())`
   — `heartbeat_task` loops forever, so the gather never completed even after
   the client disconnected and both other tasks returned, which meant the
   `finally` never ran and the billed Live session stayed open. The inline
   comment ("Exceptions from either task will propagate and cancel the other
   tasks") was also wrong: `gather` propagates the first exception but leaves
   siblings running, leaking an orphaned heartbeat per session.

   Now `asyncio.wait([upstream, downstream], return_when=FIRST_COMPLETED)`
   followed by cancelling everything, heartbeat included. The heartbeat is
   deliberately **not** in the lifecycle set — with `HEARTBEAT_ENABLED=false` it
   returns immediately, and a FIRST_COMPLETED over all three would have ended
   every session instantly.

   Two consequences for tests. **The suppression in `ws_connect` is gone** — it
   was the documented canary, and teardown is clean now; if you find yourself
   wanting it back, the endpoint has regressed. And the `run_live` stub had to
   become faithful: it now awaits forever instead of returning immediately,
   because an immediate return means "the Live API hung up" and the endpoint
   correctly tears the session down before the client's frames are read.
   `test_session_ends_when_the_client_disconnects` pins the behaviour.

2. **`extract_function_calls` is untestable where it lives.** It is a nested
   closure inside `websocket_endpoint`, so it cannot be imported. It is pure
   logic over three different event shapes (ADK `tool_call`, Live
   `server_content.model_turn`, direct `content`) and is exactly the kind of
   duck-typed fallback chain that rots silently across SDK versions. Move it to
   module level and test each shape plus their precedence.

3. **The dedup rule has no test and can't get one.** The 1.5-second window
   (`count != last_match_digit or (current_time - last_match_time) >= 1.5`) is
   inline in `downstream_task` and reads the event-loop clock directly. Extract
   it as a small function or class taking an injected clock, then test the
   boundary without sleeping.

Items 2 and 3 are the same shape of problem: `websocket_endpoint` is ~380 lines
with its logic in closures. Every extraction to module level converts an
untestable branch into a cheap unit test.

## Suggested CI

There is no CI config in the repo. Everything in tiers 1–3 runs with no
credentials, so a workflow on push costs nothing:

```bash
./scripts/install_deps.sh --dev

ruff check . && ruff format --check .
python -m pytest            # 15 hermetic tests
cd frontend && npm ci && npm run lint && npm run build
```

CI matters here beyond the tests: nothing in the current workflow ever does a
clean install, which is exactly why an unresolvable `requirements.txt` sat
undetected behind a working local environment and a `Dockerfile` that could not
build. A job that installs from scratch is what keeps `overrides.txt` honest —
if a future `google-adk` genuinely breaks against websockets 17, this is where
it surfaces.

`pytest` and `pytest-cov` are declared in `requirements-dev.txt` (kept out of
`requirements.txt` so they don't ship in the Cloud Run image). Install both with
`./scripts/install_deps.sh --dev`, which applies the `overrides.txt` websockets
override — a bare `pip install -r requirements.txt` cannot resolve.

Measured baseline from the 15 hermetic tests:

```
backend/app/biometric_agent/agent.py     96%
backend/app/main.py                      61%
TOTAL                                    68%
```

Ratchet from 68% rather than picking a target up front. The uncovered 39% of
`main.py` is almost entirely `downstream_task` — the event-shape fallbacks and
the dedup rule — which is the code that blockers 2 and 3 make untestable. Fixing
those is what moves this number, not writing more tests against the current
shape.
