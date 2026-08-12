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
| 3. Contract | The JS/Python frame-prefix agreement | free | yes (once added) |
| 4. Frontend | PCM conversion, packet framing, socket state | free | not yet — needs vitest |
| 5. Live | Real billed Live API call | billed | never; opt-in only |

Tiers 1–3 are the gate. Tiers 4–5 are opt-in.

## Tier 1 — Unit (exists, 5 tests)

`backend/app/biometric_agent/test_agent.py` already asserts properly on
`report_digit`, `trigger_system_error`, `trigger_heavy_metal_mode`, and both
`get_model_id` branches. These are real tests — CLAUDE.md's "tests can't fail"
warning applies to the **root-level** files only.

One weakness worth fixing: both `get_model_id` tests mutate `os.environ` and
`sys.argv` directly and restore by hand, so a failure mid-test leaks state into
whatever runs next. Convert to `monkeypatch.setenv` / `monkeypatch.setattr`,
which unwinds on failure.

## Tier 2 — Protocol (built, 10 tests)

`tests/conftest.py` + `tests/test_ws_protocol.py`.

Two fixtures do the work:

- **`spy`** replaces `LiveRequestQueue` with a recorder and stubs
  `runner.run_live` with an async generator that yields nothing. It keeps
  `send_realtime` and `send_content` in separate lists, which is what lets a test
  assert text never leaked into `send_realtime` — the exact regression the
  removed `patch_adk.py` used to mask.
- **`ws_connect`** opens the real endpoint over Starlette's in-process transport.

**The one trap in `ws_connect`**, caught in review and worth stating plainly
because the first version shipped it: the teardown suppression must wrap only the
`__exit__` call. Writing it as

```python
with contextlib.suppress(Exception), client.websocket_connect(path) as ws:
    yield ws
```

puts the `yield` inside the `suppress`, and `@contextmanager` re-raises the
caller's exception *at* that yield — so `suppress` eats it, `__exit__` returns
True, and **every assertion inside a `with ws_connect()` block passes
vacuously**. That is the exact anti-pattern this whole document exists to remove,
reintroduced in the fixture meant to remove it. The fixed form enters the client
manually and suppresses only in a `finally`. If you touch this fixture, re-run
the check: put `assert False` inside a `with ws_connect()` block and confirm the
test fails.

A related rule for tests here: **assert on unique strings.** `main.py:227` sends
`"Neural handshake"` unconditionally at every connect, so a test asserting that
phrase reached the queue passes even if the entire client-text branch is deleted.
The first draft of `test_text_never_reaches_send_realtime` had exactly that bug.

Covered: config frame ordering and contents, prefix `1` → `audio/pcm;rate=16000`,
prefix `2` → `image/jpeg`, header-only frames dropped, unknown prefixes ignored,
malformed JSON not fatal, base64 JSON paths, empty payloads skipped, queue closed
on disconnect.

These were checked against a mutation: flipping the input rate from 16000 to
24000 in `main.py` turns `test_prefix_1_routes_to_16khz_audio` red. They detect
change, not just exercise lines.

## Tier 3 — Contract (recommended next)

The frame protocol is written down twice and shared nowhere:

- `frontend/src/useGeminiSocket.js:185` — `packet[0] = 1`
- `frontend/src/useGeminiSocket.js:223` — `packet[0] = 2`
- `backend/app/main.py:256,268` — `if msg_type == 1` / `elif msg_type == 2`

Either end can be changed without the other noticing, and the failure is silent:
audio arrives tagged as JPEG and the model just behaves badly. Cheapest fix is a
test that reads `useGeminiSocket.js`, regexes out both `packet[0] = N`
assignments, and asserts they match the Python constants. Better fix is to name
the constants in `main.py` (`AUDIO_PREFIX = 1`, `JPEG_PREFIX = 2`) and emit them
in the existing `config` frame so the client reads them at runtime instead of
hardcoding.

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

`test_live_connection.py` stays a manual smoke check, but it currently prints
errors instead of failing, so even when you do run it deliberately it cannot tell
you the Live API broke. Two small changes make it worth its cost:

1. Move `genai.Client(...)` construction inside the test function. Right now it
   runs at import, which is why collection used to abort.
2. Replace `except Exception as e: print(...)` with an assertion that an event
   arrived.

Then mark it `@pytest.mark.live` and run it on purpose:
`python -m pytest test_live_connection.py`.

`test_ws_backend.py` / `test_ws_backend_v2.py` are superseded by Tier 2 for
protocol coverage. Keep them as manual end-to-end probes against a running
server, or delete them once Tier 2 covers the same ground against the real model.

## What `make test` does now

`pytest.ini` sets `testpaths = tests backend/app/biometric_agent`, so default
collection picks up only the hermetic suites: **15 tests, no API key, no network,
no charge, and they can fail.** The root-level smoke scripts are untouched and
run by explicit path.

Markers `live` and `needs_server` are registered for the opt-in tiers.

## Blockers found while building this

Three things surfaced that are worth fixing on their own merits:

1. **The endpoint never shuts down cleanly.** `await asyncio.gather(upstream_task(),
   downstream_task(), heartbeat_task())` — `heartbeat_task` loops forever, so the
   gather never completes even after the client disconnects and both other tasks
   return. Under a real server the ASGI layer cancels it; under test the portal
   raises `CancelledError`, which is why `ws_connect` has to suppress exceptions
   on teardown. The inline comment ("Exceptions from either task will propagate
   and cancel the other tasks") is also wrong — `gather` propagates the first
   exception but leaves siblings running. Use `asyncio.wait(...,
   return_when=FIRST_COMPLETED)` and cancel the rest, or a `TaskGroup`. **The
   suppression in `ws_connect` is the canary: delete it when this is fixed.**

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
