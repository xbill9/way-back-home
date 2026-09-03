"""The eval bundle: recorder, scorer, packager. Offline, no key, no charge.

These cover a recording round-tripping to disk, the scorer's arithmetic and
anomaly checks, that the packager produces something playable, and -- through the
same in-process transport the rest of the suite uses -- that the endpoint really
does feed the recorder and flush it on disconnect.

What none of it can prove is that the *model* side populates a recording, since
`run_live()` is stubbed: no output audio, no transcription chunks, no tool calls
arrive here. Those paths need one real run with EVAL_CAPTURE_DIR set.

The splice case is built from the real 2026-08-12 log, so the anomaly checks are
pinned against the defect they exist for rather than a hypothetical one.
"""

from __future__ import annotations

import json
import os
import sys
import tarfile
import wave
from pathlib import Path

import pytest

EVALS_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "evals"))
if EVALS_DIR not in sys.path:
    sys.path.insert(0, EVALS_DIR)

import package_submission  # noqa: E402
import score_session as scorer  # noqa: E402
from eval_capture import SessionRecorder, maybe_create_recorder  # noqa: E402


def write_session(
    tmp_path: Path,
    *,
    events: list[dict],
    ground_truth: dict | None = None,
    input_pcm: bytes = b"",
    input_rate: int = 16000,
) -> Path:
    """A session directory as the recorder would have written it."""
    session = tmp_path / "20260812T230227-abc123"
    session.mkdir(parents=True, exist_ok=True)
    with (session / "events.jsonl").open("w", encoding="utf-8") as fh:
        for event in events:
            fh.write(json.dumps(event) + "\n")
    (session / "manifest.json").write_text(
        json.dumps(
            {
                "session_id": "abc123",
                "model_id": "models/walkie-talkie",
                "duration_ms": 20000,
                "audio": {
                    "input": {
                        "path": "input_audio.pcm",
                        "sample_rate": input_rate,
                        "encoding": "s16le",
                        "channels": 1,
                    },
                    "output": {
                        "path": "output_audio.pcm",
                        "sample_rate": 24000,
                        "encoding": "s16le",
                        "channels": 1,
                    },
                },
                "versions": {"google-adk": "2.6.3"},
            }
        )
    )
    if input_pcm:
        (session / "input_audio.pcm").write_bytes(input_pcm)
    if ground_truth is not None:
        (session / "ground_truth.json").write_text(json.dumps(ground_truth))
    return session


# -- recorder ---------------------------------------------------------------


def test_recorder_is_off_without_a_capture_dir():
    assert (
        maybe_create_recorder(
            None, user_id="u", session_id="s", model_id="m", config={}
        )
        is None
    )


def test_recorder_round_trips_a_session(tmp_path):
    rec = SessionRecorder(
        tmp_path,
        user_id="user1",
        session_id="s1",
        model_id="models/walkie-talkie",
        config={"input_sample_rate": 16000, "video_fps": 1.0},
    )
    rec.add_input_audio(b"\x01\x02" * 100)
    rec.add_input_frame(b"\xff\xd8jpeg")
    rec.add_output_audio(b"\x03\x04" * 50)
    rec.add_event("function_call", name="report_digit", args={"count": 3})
    rec.add_event("output_transcription", text="Ready.", finished=True)

    out = rec.close()
    assert out is not None

    manifest = json.loads((out / "manifest.json").read_text())
    assert manifest["model_id"] == "models/walkie-talkie"
    assert manifest["audio"]["input"]["sample_rate"] == 16000
    assert manifest["audio"]["input"]["bytes"] == 200
    assert manifest["audio"]["output"]["sample_rate"] == 24000
    assert manifest["counts"]["frames"] == 1
    assert manifest["truncated"] == []

    assert (out / "input_audio.pcm").read_bytes() == b"\x01\x02" * 100
    assert (out / "frames" / "frame_0001.jpg").read_bytes() == b"\xff\xd8jpeg"

    events = [
        json.loads(line)
        for line in (out / "events.jsonl").read_text().splitlines()
        if line
    ]
    # add_input_frame records its own event, hence three.
    assert [e["kind"] for e in events] == [
        "input_frame",
        "function_call",
        "output_transcription",
    ]
    assert all(isinstance(e["t_ms"], int) for e in events)

    # The template is written so there is something to fill in.
    assert (out / "ground_truth.json").exists()


def test_untouched_template_counts_as_unlabelled(tmp_path):
    """An unedited template must not score as if its examples were real labels.

    The first real recording scored "0/1 matched, 5 spurious" purely because the
    template's example gesture had never been edited -- confident nonsense, and
    worse than reporting no labels at all.
    """
    session = write_session(
        tmp_path,
        events=[
            {
                "t_ms": 8442,
                "kind": "function_call",
                "name": "report_digit",
                "args": {"count": 1},
            }
        ],
        ground_truth={
            "_template": True,
            "expected_digits": [{"count": 3, "at_ms": 5000, "tolerance_ms": 4000}],
            "expected_utterances": ["Ready."],
        },
    )
    report = scorer.score_session(session)

    assert report["labelled"] is False
    assert "detections" not in report
    assert report["passed"] is True


def test_recorder_writes_the_template_marked_as_a_template(tmp_path):
    rec = SessionRecorder(
        tmp_path, user_id="u", session_id="s1", model_id="m", config={}
    )
    rec.add_event("noop")
    out = rec.close()

    gt = json.loads((out / "ground_truth.json").read_text())
    assert gt["_template"] is True
    # ...and the scorer honours it, so the two halves cannot drift apart.
    assert scorer.load_ground_truth(out) is None


def test_recorder_never_overwrites_hand_written_labels(tmp_path):
    def record():
        rec = SessionRecorder(
            tmp_path, user_id="u", session_id="s1", model_id="m", config={}
        )
        rec.add_event("noop")
        return rec.close()

    out = record()
    labels = out / "ground_truth.json"
    labels.write_text('{"notes": "three fingers, by hand"}')

    # Same session id and dir name -> second write must not clobber the labels.
    again = record()
    assert again == out
    assert json.loads(labels.read_text())["notes"] == "three fingers, by hand"


def test_recorder_caps_buffers_and_says_so(tmp_path, monkeypatch):
    monkeypatch.setattr("eval_capture.MAX_FRAMES", 2)
    rec = SessionRecorder(
        tmp_path, user_id="u", session_id="s1", model_id="m", config={}
    )
    for _ in range(5):
        rec.add_input_frame(b"jpeg")

    out = rec.close()
    manifest = json.loads((out / "manifest.json").read_text())
    assert manifest["counts"]["frames"] == 2
    # A truncated capture must not look complete.
    assert "frames" in manifest["truncated"]


def test_recorder_close_swallows_write_errors(tmp_path, monkeypatch):
    rec = SessionRecorder(
        tmp_path, user_id="u", session_id="s1", model_id="m", config={}
    )
    monkeypatch.setattr(
        rec, "_write", lambda: (_ for _ in ()).throw(OSError("disk full"))
    )
    # Teardown runs right after the billed session closes; an exception here
    # would mask whatever actually ended the session.
    assert rec.close() is None


# -- the wiring in main.py --------------------------------------------------
#
# The unit tests above prove the recorder works when called. These prove the
# endpoint calls it, over the same in-process transport as the rest of the suite.


class SpyLike:
    """Minimal LiveRequestQueue stand-in for the tests that drive run_live.

    The shared `spy` fixture also stubs run_live, which these tests need to
    control, so they bring their own queue.
    """

    def send_realtime(self, blob):
        pass

    def send_content(self, content, partial=False):
        pass

    def close(self):
        pass


def only_session_dir(root: Path) -> Path:
    dirs = [p.parent for p in root.glob("*/events.jsonl")]
    assert len(dirs) == 1, f"expected one recording under {root}, got {dirs}"
    return dirs[0]


def test_endpoint_records_a_session_and_flushes_on_disconnect(
    main_module, monkeypatch, spy, ws_connect, tmp_path
):
    monkeypatch.setattr(main_module, "EVAL_CAPTURE_DIR", str(tmp_path))

    with ws_connect("/ws/u1/s-rec") as ws:
        ws.receive_text()  # config frame
        ws.send_text(json.dumps({"type": "audio_config", "sample_rate": 48000}))
        ws.send_bytes(bytes([main_module.AUDIO_PREFIX]) + b"\x00\x01" * 8)
        ws.send_bytes(bytes([main_module.JPEG_PREFIX]) + b"\xff\xd8jpeg")
        ws.send_text(json.dumps({"type": "text", "text": "probe"}))

    # Written in the session teardown, so it must exist by the time the client
    # context has exited -- the same guarantee the queue-closed test relies on.
    session = only_session_dir(tmp_path)

    manifest = json.loads((session / "manifest.json").read_text())
    assert manifest["session_id"] == "s-rec"
    assert manifest["model_id"] == main_module.MODEL_ID
    # The rate the browser actually granted, not the one we asked for: recording
    # 48 kHz samples as 16 kHz would make the audio useless to whoever gets it.
    assert manifest["audio"]["input"]["sample_rate"] == 48000

    assert (session / "input_audio.pcm").read_bytes() == b"\x00\x01" * 8
    assert (session / "frames" / "frame_0001.jpg").read_bytes() == b"\xff\xd8jpeg"

    events = [
        json.loads(line)
        for line in (session / "events.jsonl").read_text().splitlines()
        if line
    ]
    kinds = [e["kind"] for e in events]
    assert "input_frame" in kinds
    assert {"kind": "user_text", "text": "probe"}.items() <= next(
        e for e in events if e["kind"] == "user_text"
    ).items()

    assert (session / "ground_truth.json").exists()


def test_endpoint_records_the_model_turn_from_event_content(
    main_module, monkeypatch, ws_connect, tmp_path
):
    """Model audio and text come from `event.content`, not `event.server_content`.

    Pinning the field is the whole point. The endpoint used to probe
    `event.server_content.model_turn` -- the raw Live API shape, which ADK's
    Event does not have and, being `extra="ignore"`, does not complain about. It
    silently matched nothing: no GEMINI TEXT line, no audio counter, and a
    0-byte output_audio.pcm in the first real recording. Nothing failed, which is
    exactly why this test exists.
    """
    from google.adk.events import Event
    from google.genai import types as genai_types

    monkeypatch.setattr(main_module, "EVAL_CAPTURE_DIR", str(tmp_path))
    monkeypatch.setattr(main_module, "LiveRequestQueue", lambda *a, **k: SpyLike())

    async def one_model_turn(**kwargs):
        yield Event(
            id=Event.new_id(),
            invocation_id="inv-1",
            author="model",
            content=genai_types.Content(
                role="model",
                parts=[
                    genai_types.Part(
                        inline_data=genai_types.Blob(
                            mime_type="audio/pcm;rate=24000", data=b"\x10\x20" * 40
                        )
                    ),
                    genai_types.Part(text="Ready."),
                ],
            ),
        )

    monkeypatch.setattr(main_module.runner, "run_live", one_model_turn)

    with ws_connect("/ws/u1/s-model") as ws:
        ws.receive_text()  # config
        # Model audio now rides its own binary frame, prefixed, rather than as
        # base64 inside the event JSON -- that encoding was a third of the
        # downlink. The event still follows, minus its inlineData.
        # The wire carries mu-law now; the recorder still stores the PCM the
        # model sent, which is the point of a recording.
        from audio_codec import pcm16_to_ulaw

        audio_frame = ws.receive_bytes()
        assert audio_frame[0] == main_module.MODEL_AUDIO_PREFIX
        assert audio_frame[1:] == pcm16_to_ulaw(b"\x10\x20" * 40)
        forwarded = json.loads(ws.receive_text())
        assert not any(
            part.get("inlineData")
            for part in (forwarded.get("content") or {}).get("parts") or []
        ), "audio must not also be sent inside the JSON"

    session = only_session_dir(tmp_path)
    assert (session / "output_audio.pcm").read_bytes() == b"\x10\x20" * 40

    manifest = json.loads((session / "manifest.json").read_text())
    assert manifest["audio"]["output"]["bytes"] == 80

    events = [
        json.loads(line)
        for line in (session / "events.jsonl").read_text().splitlines()
        if line
    ]
    assert any(e["kind"] == "model_text" and e["text"] == "Ready." for e in events)


def test_endpoint_does_not_record_user_content_as_model_output(
    main_module, monkeypatch, ws_connect, tmp_path
):
    """A user-role event must not be filed as the model's turn."""
    from google.adk.events import Event
    from google.genai import types as genai_types

    monkeypatch.setattr(main_module, "EVAL_CAPTURE_DIR", str(tmp_path))
    monkeypatch.setattr(main_module, "LiveRequestQueue", lambda *a, **k: SpyLike())

    async def one_user_turn(**kwargs):
        yield Event(
            id=Event.new_id(),
            invocation_id="inv-1",
            author="user",
            content=genai_types.Content(
                role="user", parts=[genai_types.Part(text="Neural handshake")]
            ),
        )

    monkeypatch.setattr(main_module.runner, "run_live", one_user_turn)

    with ws_connect("/ws/u1/s-user") as ws:
        ws.receive_text()
        ws.receive_text()

    session = only_session_dir(tmp_path)
    events = [
        json.loads(line)
        for line in (session / "events.jsonl").read_text().splitlines()
        if line
    ]
    assert not [e for e in events if e["kind"] == "model_text"]
    assert not (session / "output_audio.pcm").exists()


def test_endpoint_records_nothing_when_capture_is_off(
    main_module, monkeypatch, spy, ws_connect, tmp_path
):
    """Default is off: a stray restart must not start recording the microphone."""
    monkeypatch.setattr(main_module, "EVAL_CAPTURE_DIR", None)

    with ws_connect("/ws/u1/s-off") as ws:
        ws.receive_text()
        ws.send_bytes(bytes([main_module.AUDIO_PREFIX]) + b"\x00\x01" * 8)

    assert list(tmp_path.iterdir()) == []


# -- scorer -----------------------------------------------------------------


def test_scorer_matches_detections_and_reports_latency(tmp_path):
    session = write_session(
        tmp_path,
        events=[
            {
                "t_ms": 5410,
                "kind": "function_call",
                "name": "report_digit",
                "args": {"count": 3},
            },
            {
                "t_ms": 12200,
                "kind": "function_call",
                "name": "report_digit",
                "args": {"count": 2},
            },
            {
                "t_ms": 1000,
                "kind": "output_transcription",
                "text": "Ready.",
                "finished": True,
            },
        ],
        ground_truth={
            "expected_digits": [
                {"count": 3, "at_ms": 5000, "tolerance_ms": 4000},
                {"count": 2, "at_ms": 12000, "tolerance_ms": 4000},
            ],
            "expected_utterances": ["Ready."],
            "forbidden_utterances": [],
        },
    )
    report = scorer.score_session(session)

    assert report["passed"]
    assert report["detections"]["matched"] == 2
    assert report["detections"]["latency_ms"]["min"] == 200
    assert report["detections"]["latency_ms"]["max"] == 410
    assert report["utterances"]["missing"] == []
    assert report["anomalies"] == []


def test_scorer_flags_missed_late_and_spurious_detections(tmp_path):
    session = write_session(
        tmp_path,
        events=[
            # Too late for the 2s tolerance -> the expectation is missed and the
            # call itself is spurious.
            {
                "t_ms": 30000,
                "kind": "function_call",
                "name": "report_digit",
                "args": {"count": 3},
            },
            # Never asked for.
            {
                "t_ms": 4000,
                "kind": "function_call",
                "name": "report_digit",
                "args": {"count": 5},
            },
        ],
        ground_truth={
            "expected_digits": [{"count": 3, "at_ms": 5000, "tolerance_ms": 2000}],
            "expected_utterances": [],
        },
    )
    report = scorer.score_session(session)

    assert not report["passed"]
    assert report["detections"]["matched"] == 0
    assert [m["count"] for m in report["detections"]["missed"]] == [3]
    assert sorted(s["count"] for s in report["detections"]["spurious"]) == [3, 5]


def test_scorer_reads_the_digit_alias(tmp_path):
    """report_digit has been seen with `digit` as well as `count`."""
    session = write_session(
        tmp_path,
        events=[
            {
                "t_ms": 5100,
                "kind": "function_call",
                "name": "report_digit",
                "args": {"digit": 4},
            },
        ],
        ground_truth={"expected_digits": [{"count": 4, "at_ms": 5000}]},
    )
    assert scorer.score_session(session)["detections"]["matched"] == 1


def test_scorer_ignores_other_tools(tmp_path):
    session = write_session(
        tmp_path,
        events=[
            {
                "t_ms": 3000,
                "kind": "function_call",
                "name": "trigger_heavy_metal_mode",
                "args": {},
            },
        ],
        ground_truth={"expected_digits": [], "expected_utterances": []},
    )
    report = scorer.score_session(session)
    assert report["detections"]["spurious"] == []
    assert report["passed"]


def test_scorer_flags_forbidden_utterance(tmp_path):
    session = write_session(
        tmp_path,
        events=[
            {
                "t_ms": 900,
                "kind": "output_transcription",
                "text": "Access granted.",
                "finished": True,
            },
        ],
        ground_truth={
            "expected_digits": [],
            "expected_utterances": [],
            "forbidden_utterances": ["access granted"],
        },
    )
    report = scorer.score_session(session)
    assert report["utterances"]["forbidden_present"] == ["access granted"]
    assert not report["passed"]


def test_scorer_works_without_labels(tmp_path):
    session = write_session(
        tmp_path,
        events=[
            {"t_ms": 10, "kind": "output_transcription", "text": "hi", "finished": True}
        ],
    )
    report = scorer.score_session(session)
    assert report["labelled"] is False
    assert "detections" not in report
    assert report["passed"] is True


# -- the anomaly this whole thing exists for --------------------------------

# Verbatim from the 2026-08-12 log: an unrelated markdown-formatted essay
# spliced into "Stabilize hand." mid-word.
SPLICE_TAIL = (
    "ged that this was not a matter of the law, but of conscience.\n"
    "*   **Abolitionism:** Many Quakers were among the first to protest.\n"
    "### 4. The Impact of Their Decision\n"
)


def test_scorer_catches_the_real_splice(tmp_path):
    session = write_session(
        tmp_path,
        events=[
            {
                "t_ms": 6000,
                "kind": "output_transcription",
                "text": "Stabilize hand.",
                "finished": False,
                "live_session_id": "sess-1",
            },
            {
                "t_ms": 6050,
                "kind": "output_transcription",
                "text": SPLICE_TAIL,
                "finished": False,
                "live_session_id": "sess-2",
            },
            {
                "t_ms": 6100,
                "kind": "output_transcription",
                "text": "Stabilize hand." + SPLICE_TAIL,
                "finished": True,
                "live_session_id": "sess-1",
            },
        ],
        ground_truth={"expected_digits": [], "expected_utterances": []},
    )
    report = scorer.score_session(session)
    kinds = {a["type"] for a in report["anomalies"]}

    assert "markdown_in_audio_transcript" in kinds
    assert "midword_splice" in kinds
    assert "inconsistent_stream_identity" in kinds
    # Anomalies alone must fail the session, even with every label satisfied.
    assert not report["passed"]


@pytest.mark.parametrize(
    "text",
    [
        "Ready.",
        "Stabilize hand. Show me three fingers.",
        "I see 2 fingers. Access granted.",
        # A spoken aside that merely contains punctuation must not trip it.
        "Wait -- that is a 5, not a 4.",
    ],
)
def test_scorer_does_not_flag_ordinary_speech(tmp_path, text):
    session = write_session(
        tmp_path,
        events=[
            {
                "t_ms": 1000,
                "kind": "output_transcription",
                "text": text,
                "finished": True,
                "live_session_id": "sess-1",
            },
        ],
    )
    assert scorer.score_session(session)["anomalies"] == []


# -- packager ---------------------------------------------------------------


def test_packager_writes_wav_at_the_recorded_rate(tmp_path):
    """The browser does not always grant 16 kHz, so the rate comes from the
    manifest -- a wrong header makes the speech unusable to whoever receives it.
    """
    session = write_session(
        tmp_path,
        events=[
            {"t_ms": 10, "kind": "output_transcription", "text": "hi", "finished": True}
        ],
        ground_truth={"expected_digits": [], "expected_utterances": []},
        input_pcm=b"\x00\x01" * 480,
        input_rate=48000,
    )
    bundle = tmp_path / "out" / "submission.tar.gz"
    summary = package_submission.build([session], bundle)

    assert summary["sessions"] == 1
    assert summary["labelled"] == 1
    assert bundle.exists()

    with tarfile.open(bundle) as tar:
        names = tar.getnames()
        assert f"{session.name}/input_audio.wav" in names
        assert f"{session.name}/manifest.json" in names
        assert f"{session.name}/score.json" in names
        assert "report.json" in names
        assert "SUBMISSION.md" in names

        extracted = tar.extractfile(f"{session.name}/input_audio.wav").read()

    wav_path = tmp_path / "check.wav"
    wav_path.write_bytes(extracted)
    with wave.open(str(wav_path)) as wav:
        assert wav.getframerate() == 48000
        assert wav.getnchannels() == 1
        assert wav.getsampwidth() == 2
        assert wav.getnframes() == 480


def test_packager_can_leave_out_audio_and_frames(tmp_path):
    session = write_session(
        tmp_path,
        events=[{"t_ms": 10, "kind": "input_frame", "index": 1, "bytes": 4}],
        input_pcm=b"\x00\x01" * 100,
    )
    (session / "frames").mkdir()
    (session / "frames" / "frame_0001.jpg").write_bytes(b"\xff\xd8jpeg")

    bundle = tmp_path / "no-media.tar.gz"
    package_submission.build(
        [session], bundle, include_audio=False, include_frames=False
    )

    with tarfile.open(bundle) as tar:
        names = tar.getnames()
        body = tar.extractfile("SUBMISSION.md").read().decode()

    assert not [n for n in names if n.endswith((".wav", ".jpg"))]
    # The events survive, which is where the model-quality signal lives.
    assert f"{session.name}/events.jsonl" in names
    assert "audio: **excluded from this bundle**" in body


def test_submission_md_surfaces_the_anomaly(tmp_path):
    session = write_session(
        tmp_path,
        events=[
            {
                "t_ms": 6000,
                "kind": "output_transcription",
                "text": "Stabilize hand." + SPLICE_TAIL,
                "finished": True,
            },
        ],
        ground_truth={"expected_digits": [], "expected_utterances": []},
    )
    bundle = tmp_path / "flagged.tar.gz"
    summary = package_submission.build([session], bundle)

    assert summary["with_anomalies"] == 1
    assert summary["passed"] == 0

    with tarfile.open(bundle) as tar:
        body = tar.extractfile("SUBMISSION.md").read().decode()

    assert "Anomalies" in body
    assert "markdown_in_audio_transcript" in body
    assert "models/walkie-talkie" in body
