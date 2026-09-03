# Session recordings, evals and ground truths

The EAP asked for "sample audio files, evals and ground truths to help us
improve the quality of the models". None of that could be produced from what was
here before: `make test` stubs `run_live()` and never contacts a model, so it
measures the wire format, not model quality, and the only audio in the repo was
`mock/mock_audio.pcm` — three seconds of *model output*, the canned greeting that
was deliberately removed from the startup path.

So this directory records real sessions, lets you label what actually happened,
and scores the model against those labels. The output is one `.tar.gz` to upload.

## The loop

```bash
# 1. Record. Off unless EVAL_CAPTURE_DIR is set -- this captures your
#    microphone and camera.
EVAL_CAPTURE_DIR=~/eval-captures make run
#    ...then use the app in the browser, doing gestures you can describe later.

# 2. Label. One directory per session; fill in the template it wrote.
$EDITOR ~/eval-captures/*/ground_truth.json

# 3. Score.
make eval                      # or: python evals/score_session.py --all ~/eval-captures

# 4. Package. Prints the path of the file to upload.
make submission                # add --no-audio by hand to omit voice
```

`make eval` and `make submission` default to `~/eval-captures`; override with
`EVAL_CAPTURE_DIR=...`.

## What a recording contains

One directory per session, named `<started>-<session_id>`:

| File | Contents |
|---|---|
| `manifest.json` | model id, run config, library versions, duration, audio formats |
| `events.jsonl` | one event per line, `t_ms` from session start |
| `input_audio.pcm` | your microphone, s16le mono at whatever rate the browser granted |
| `output_audio.pcm` | the model's audio, s16le mono 24 kHz |
| `frames/*.jpg` | the JPEG frames as sent, 1 FPS |
| `ground_truth.json` | the labels — written as a template, then yours |

`events.jsonl` carries transcription chunks (**partial and finished**), function
calls, model text, `turn_complete` / `interrupted`, and frame arrivals.

Two details that matter for interpreting a recording:

- **Transcription is recorded per chunk, not just per finished turn.** The
  accumulated `finished` text cannot show where a splice began, which is exactly
  what went wrong on 2026-08-12. Each chunk keeps its `live_session_id`,
  `interaction_id` and `model_version`.
- **Function calls are recorded before the app's own duplicate suppression**, so
  what gets scored is the model's raw behaviour, not what survived our dedup.

Audio is written headerless because that is what the wire carries. The packager
converts it to WAV using the rate from the manifest — the browser does not always
grant 16 kHz, and a `.pcm` played at the wrong rate sounds like a different bug.

## Writing ground truth

The recorder writes the file with a `"_template": true` line. **Delete that line
when you fill it in** — while it is there the scorer treats the session as
unlabelled, because scoring against the template's example values reports
confident nonsense (the first real run came back "0/1 matched, 5 spurious" purely
because nobody had edited it).

```json
{
  "notes": "Three fingers at ~5s, then two at ~12s. Good lighting.",
  "expected_digits": [
    {"count": 3, "at_ms": 5000, "tolerance_ms": 4000},
    {"count": 2, "at_ms": 12000, "tolerance_ms": 4000}
  ],
  "expected_utterances": ["Scanner Online."],
  "forbidden_utterances": []
}
```

`at_ms` is when you made the gesture, from session start; `tolerance_ms` is how
late a detection may be and still count. Rough timings are fine — that is what
the tolerance is for. A session with no `ground_truth.json` still gets anomaly
checks, and the packager warns that it went in unscored.

## What the scorer reports

- **detections** — did `report_digit` fire, with the right count, in time.
  Misses and spurious calls are listed; latency is reported per match.
- **utterances** — expected substrings present, forbidden ones absent.
- **anomalies** — machine-checkable defects in the output stream itself.

The anomaly checks are the part worth explaining, because they are why this is
more than an accuracy script:

| Check | What it means |
|---|---|
| `markdown_in_audio_transcript` | An *audio* transcript containing markdown headings, bullets or bold. Nobody pronounces `###`. The app requests AUDIO only and never sends markdown, so it cannot originate on this side. |
| `midword_splice` | One chunk ends a sentence, the next resumes mid-word — `"hand."` then `"ged that…"`. The splice signature itself. |
| `inconsistent_stream_identity` | `live_session_id`, `interaction_id` or `model_version` changed mid-stream. Distinguishes "the model rambled" from "another generation arrived here". **Dormant on ADK 2.6.3** — see below. |

`inconsistent_stream_identity` cannot currently fire. ADK builds every live event
as `Event(id=..., invocation_id=..., author=...)` and never copies those three
fields off the `LlmResponse`, so all three arrive as `None` — confirmed against a
real session on 2026-08-12, and the same class of gap as `interaction_status`.
The recorder stores them anyway, so the check starts working the day ADK
populates them. Until then, a clean run is not evidence that the stream was
consistent.

Anomalies fail a session on their own, even when every label is satisfied, and
`score_session.py` exits non-zero — so it can gate a bundle before upload.

## Before you upload

The recordings contain your voice and your camera. `--no-audio` and `--no-frames`
drop them and keep the events, labels and scores, which is where the
model-quality signal lives. Worth a deliberate decision rather than shipping
whatever is on disk — particularly for any session where an anomaly suggests the
stream carried content that was not yours.

## Tests

`tests/test_eval_capture.py` covers the recorder, scorer and packager offline
(part of `make test`). The splice case is built from the real 2026-08-12 log, so
the anomaly checks are pinned against the defect they exist for. What the suite
cannot prove is that a live session populates the recorder correctly — that needs
one real run with `EVAL_CAPTURE_DIR` set.
