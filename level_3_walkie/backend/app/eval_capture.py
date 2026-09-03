"""Record a real Live session to disk so it can be labelled and scored.

This exists because the EAP asked for "sample audio files, evals and ground
truths", and none of those can be produced from the hermetic test suite: it
stubs `run_live()` and never contacts a model, so it proves the wire format and
nothing about model quality. The only place real input audio exists is inside a
live session, between the browser and the model -- hence a recorder in the
request path rather than a standalone script.

Off unless `EVAL_CAPTURE_DIR` is set. It records the user's microphone and
camera, so it is opt-in by design and announces itself in the log when it starts.

Everything is buffered in memory and written once at session teardown. Two
reasons: per-frame disk I/O in the event loop would compete with a latency-
sensitive audio stream, and a session is small enough that it does not matter
(16 kHz mono PCM is 32 KB/s, so even the 2-minute audio+video ceiling is well
under 4 MB). The cost is that a hard kill loses the recording -- a clean client
disconnect, which is the normal case, does not.

Layout, one directory per session:

    <EVAL_CAPTURE_DIR>/<started>-<session_id>/
        manifest.json      model, config, versions, counts, wall-clock start
        events.jsonl       one JSON object per event, `t_ms` from session start
        input_audio.pcm    raw client PCM, rate in the manifest
        output_audio.pcm   raw model PCM (24 kHz)
        frames/*.jpg       the JPEG frames as sent
        ground_truth.json  template, written only if absent -- fill it in by hand
"""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Any

# The scanner's opening line, defined here because main.py imports this module
# (so the dependency runs one way) and the eval template has to agree with what
# the model is actually asked to say.
#
# It must not contain "scan". The wake word matches `scan` as a substring, the
# greeting is played through the user's speakers, and a microphone in the same
# room turns "Scanner Online." into a phantom scan request -- one that lands
# while the model is still answering the previous one.
GREETING = "Ready."

logger = logging.getLogger(__name__)

# Buffer ceilings. A recording is a diagnostic artefact, not a reason to run the
# box out of memory on a session someone left open. Both are logged when hit,
# and the manifest records the fact -- a truncated capture that looks complete
# would be worse than no capture.
MAX_INPUT_AUDIO_BYTES = 64 * 1024 * 1024  # ~34 min of 16 kHz mono PCM
MAX_OUTPUT_AUDIO_BYTES = 64 * 1024 * 1024
MAX_FRAMES = 900  # 15 min at the 1 FPS ceiling
MAX_EVENTS = 50_000


class SessionRecorder:
    """Buffers one session in memory, then writes it out in `close()`."""

    def __init__(
        self,
        root: Path,
        *,
        user_id: str,
        session_id: str,
        model_id: str,
        config: dict[str, Any],
    ) -> None:
        self.root = Path(root)
        self.user_id = user_id
        self.session_id = session_id
        self.model_id = model_id
        self.config = dict(config)

        # Wall clock for the manifest, monotonic for offsets: the two are not
        # interchangeable, and a clock step mid-session must not reorder events.
        self.started_wall = time.time()
        self._t0 = time.monotonic()

        self._events: list[dict[str, Any]] = []
        self._input_audio = bytearray()
        self._output_audio = bytearray()
        self._frames: list[bytes] = []

        self.truncated: list[str] = []

    # -- capture -------------------------------------------------------------

    def _t_ms(self) -> int:
        return int((time.monotonic() - self._t0) * 1000)

    def _note_truncation(self, what: str) -> None:
        if what not in self.truncated:
            self.truncated.append(what)
            logger.warning(
                f"Eval capture: {what} buffer full, further data dropped "
                f"(session {self.session_id})"
            )

    def add_event(self, kind: str, **fields: Any) -> None:
        """Record one timestamped event. `kind` is the discriminator."""
        if len(self._events) >= MAX_EVENTS:
            self._note_truncation("events")
            return
        self._events.append({"t_ms": self._t_ms(), "kind": kind, **fields})

    def add_input_audio(self, payload: bytes) -> None:
        if len(self._input_audio) + len(payload) > MAX_INPUT_AUDIO_BYTES:
            self._note_truncation("input audio")
            return
        self._input_audio.extend(payload)

    def add_output_audio(self, payload: bytes) -> None:
        if len(self._output_audio) + len(payload) > MAX_OUTPUT_AUDIO_BYTES:
            self._note_truncation("output audio")
            return
        self._output_audio.extend(payload)

    def add_input_frame(self, payload: bytes) -> None:
        if len(self._frames) >= MAX_FRAMES:
            self._note_truncation("frames")
            return
        self._frames.append(payload)
        self.add_event("input_frame", index=len(self._frames), bytes=len(payload))

    # -- write ---------------------------------------------------------------

    @property
    def _dir_name(self) -> str:
        stamp = time.strftime("%Y%m%dT%H%M%S", time.localtime(self.started_wall))
        return f"{stamp}-{self.session_id}"

    def close(self) -> Path | None:
        """Write the session out. Returns the directory, or None on failure.

        Never raises: this runs in the session teardown path, where the billed
        Live session has just been closed and an exception would mask whatever
        actually ended the session.
        """
        try:
            return self._write()
        except Exception as e:  # teardown must not fail
            logger.error(f"Eval capture failed to write: {e}")
            return None

    def _write(self) -> Path:
        out = self.root / self._dir_name
        out.mkdir(parents=True, exist_ok=True)

        if self._input_audio:
            (out / "input_audio.pcm").write_bytes(bytes(self._input_audio))
        if self._output_audio:
            (out / "output_audio.pcm").write_bytes(bytes(self._output_audio))

        if self._frames:
            frames_dir = out / "frames"
            frames_dir.mkdir(exist_ok=True)
            for i, frame in enumerate(self._frames, start=1):
                (frames_dir / f"frame_{i:04d}.jpg").write_bytes(frame)

        with (out / "events.jsonl").open("w", encoding="utf-8") as fh:
            for event in self._events:
                fh.write(json.dumps(event, ensure_ascii=False) + "\n")

        input_rate = int(self.config.get("input_sample_rate") or 16000)
        manifest = {
            "session_id": self.session_id,
            "user_id": self.user_id,
            "model_id": self.model_id,
            "started_utc": time.strftime(
                "%Y-%m-%dT%H:%M:%SZ", time.gmtime(self.started_wall)
            ),
            "duration_ms": self._t_ms(),
            "config": self.config,
            "audio": {
                # Both formats are fixed by the Live API, not by us: 16 kHz in
                # (whatever the browser actually granted, which the client
                # reports) and 24 kHz out.
                "input": {
                    "path": "input_audio.pcm",
                    "encoding": "s16le",
                    "sample_rate": input_rate,
                    "channels": 1,
                    "bytes": len(self._input_audio),
                },
                "output": {
                    "path": "output_audio.pcm",
                    "encoding": "s16le",
                    "sample_rate": 24000,
                    "channels": 1,
                    "bytes": len(self._output_audio),
                },
            },
            "counts": {
                "events": len(self._events),
                "frames": len(self._frames),
            },
            "versions": _versions(),
            "truncated": self.truncated,
        }
        (out / "manifest.json").write_text(
            json.dumps(manifest, indent=2) + "\n", encoding="utf-8"
        )

        # Never overwrite labels: the template is a starting point, and a second
        # run in the same directory must not silently discard hand-written work.
        gt_path = out / "ground_truth.json"
        if not gt_path.exists():
            gt_path.write_text(
                json.dumps(_ground_truth_template(self.session_id), indent=2) + "\n",
                encoding="utf-8",
            )

        logger.info(f"Eval capture written: {out}")
        return out


def _versions() -> dict[str, str]:
    """Library versions, so a recording stays interpretable after an upgrade."""
    versions: dict[str, str] = {}
    for name, module in (
        ("google-adk", "google.adk"),
        ("google-genai", "google.genai"),
    ):
        try:
            versions[name] = __import__(module, fromlist=["__version__"]).__version__
        except Exception:  # a missing __version__ is not fatal
            versions[name] = "unknown"
    return versions


def _ground_truth_template(session_id: str) -> dict[str, Any]:
    return {
        # Delete this line once you have filled the file in. While it is present
        # the scorer treats the session as unlabelled: an untouched template
        # otherwise scores as if its example values were real ground truth, which
        # is worse than no labels at all -- it reports confident nonsense.
        "_template": True,
        "session_id": session_id,
        "notes": "What was actually in front of the camera, in plain English.",
        "_help": {
            "expected_digits": (
                "One entry per gesture you actually made. `at_ms` is when you made"
                " it, measured from session start; `tolerance_ms` is how late a"
                " detection may be and still count."
            ),
            "expected_utterances": (
                "Substrings the model should have said, matched against the output"
                " transcript."
            ),
            "forbidden_utterances": (
                "Substrings that must NOT appear. Use for regressions."
            ),
        },
        "expected_digits": [{"count": 3, "at_ms": 5000, "tolerance_ms": 4000}],
        "expected_utterances": [GREETING],
        "forbidden_utterances": [],
    }


def maybe_create_recorder(
    capture_dir: str | None,
    *,
    user_id: str,
    session_id: str,
    model_id: str,
    config: dict[str, Any],
) -> SessionRecorder | None:
    """Build a recorder if capture is switched on, else None.

    Returning None rather than a no-op object keeps the cost at one `if` per
    frame in the hot path.
    """
    if not capture_dir:
        return None
    recorder = SessionRecorder(
        Path(capture_dir).expanduser(),
        user_id=user_id,
        session_id=session_id,
        model_id=model_id,
        config=config,
    )
    logger.info(
        f"Eval capture ON -- recording microphone and camera for this session to "
        f"{recorder.root / recorder._dir_name}"
    )
    return recorder
