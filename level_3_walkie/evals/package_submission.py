#!/usr/bin/env python3
"""Bundle recorded sessions into one .tar.gz to upload to the EAP.

Usage:
    python evals/package_submission.py --all ~/eval-captures
    python evals/package_submission.py --all ~/eval-captures --no-audio
    python evals/package_submission.py <session_dir> -o submission.tar.gz

What goes in, per session: manifest.json, events.jsonl, ground_truth.json, the
JPEG frames, and the audio as **WAV** rather than raw PCM. The recorder writes
headerless s16le because that is what the wire carries and writing it is free in
the request path; a bare .pcm is unplayable without being told the rate, so the
header is added here where it costs nothing. The original rate comes from the
manifest, not a guess -- the browser does not always grant 16 kHz.

Also written: report.json from score_session.py, and SUBMISSION.md describing
the environment and anything the scorer flagged.

Audio and video are the user's microphone and camera. `--no-audio` and
`--no-frames` drop them, keeping the events, labels and scores -- which is the
part that carries the model-quality signal anyway.
"""

from __future__ import annotations

import argparse
import io
import json
import sys
import tarfile
import time
import wave
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

from score_session import score_session


def pcm_to_wav(pcm: bytes, sample_rate: int, channels: int = 1) -> bytes:
    """Wrap headerless s16le PCM in a WAV container."""
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wav:
        wav.setnchannels(channels)
        wav.setsampwidth(2)  # s16
        wav.setframerate(sample_rate)
        wav.writeframes(pcm)
    return buf.getvalue()


def _add_bytes(tar: tarfile.TarFile, arcname: str, payload: bytes) -> None:
    info = tarfile.TarInfo(arcname)
    info.size = len(payload)
    info.mtime = int(time.time())
    tar.addfile(info, io.BytesIO(payload))


def _add_audio(
    tar: tarfile.TarFile, session_dir: Path, arc_root: str, manifest: dict[str, Any]
) -> list[str]:
    """Add both audio streams as WAV. Returns the names added."""
    added = []
    for side in ("input", "output"):
        spec = (manifest.get("audio") or {}).get(side) or {}
        pcm_path = session_dir / spec.get("path", f"{side}_audio.pcm")
        if not pcm_path.exists() or pcm_path.stat().st_size == 0:
            continue
        rate = int(spec.get("sample_rate") or (16000 if side == "input" else 24000))
        wav = pcm_to_wav(pcm_path.read_bytes(), rate)
        name = f"{arc_root}/{side}_audio.wav"
        _add_bytes(tar, name, wav)
        added.append(name)
    return added


def build(
    session_dirs: list[Path],
    out_path: Path,
    *,
    include_audio: bool = True,
    include_frames: bool = True,
) -> dict[str, Any]:
    reports = [score_session(d) for d in session_dirs]

    summary: dict[str, Any] = {
        "created_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "sessions": len(session_dirs),
        "labelled": sum(1 for r in reports if r["labelled"]),
        "passed": sum(1 for r in reports if r["passed"]),
        "with_anomalies": sum(1 for r in reports if r["anomalies"]),
        "audio_included": include_audio,
        "frames_included": include_frames,
    }

    out_path.parent.mkdir(parents=True, exist_ok=True)
    with tarfile.open(out_path, "w:gz") as tar:
        for session_dir, report in zip(session_dirs, reports, strict=True):
            arc_root = session_dir.name
            manifest_path = session_dir / "manifest.json"
            manifest = (
                json.loads(manifest_path.read_text(encoding="utf-8"))
                if manifest_path.exists()
                else {}
            )

            for name in ("manifest.json", "events.jsonl", "ground_truth.json"):
                path = session_dir / name
                if path.exists():
                    tar.add(path, arcname=f"{arc_root}/{name}")

            if include_audio:
                _add_audio(tar, session_dir, arc_root, manifest)

            if include_frames:
                frames = sorted((session_dir / "frames").glob("*.jpg"))
                for frame in frames:
                    tar.add(frame, arcname=f"{arc_root}/frames/{frame.name}")

            _add_bytes(
                tar,
                f"{arc_root}/score.json",
                (json.dumps(report, indent=2) + "\n").encode("utf-8"),
            )

        _add_bytes(
            tar, "report.json", (json.dumps(reports, indent=2) + "\n").encode("utf-8")
        )
        _add_bytes(
            tar, "SUBMISSION.md", _submission_md(reports, summary).encode("utf-8")
        )

    summary["bundle"] = str(out_path)
    summary["bundle_bytes"] = out_path.stat().st_size
    return summary


def _submission_md(reports: list[dict[str, Any]], summary: dict[str, Any]) -> str:
    models = sorted({r.get("model_id") or "unknown" for r in reports})
    versions: dict[str, str] = {}
    for report in reports:
        manifest = Path(report["session_dir"]) / "manifest.json"
        if manifest.exists():
            versions.update(
                json.loads(manifest.read_text(encoding="utf-8")).get("versions", {})
            )

    lines = [
        "# Live API EAP -- session recordings, evals and ground truths",
        "",
        f"Created: {summary['created_utc']}",
        f"Models: {', '.join(models)}",
        f"Sessions: {summary['sessions']} "
        f"({summary['labelled']} with hand-written ground truth)",
        f"Passing: {summary['passed']}/{summary['sessions']}",
        "",
        "## Contents",
        "",
        "One directory per session:",
        "",
        "- `manifest.json` -- model, run config, library versions, durations",
        "- `events.jsonl` -- timestamped events (`t_ms` from session start):",
        "  transcription chunks (partial and finished), function calls, model text,",
        "  turn_complete / interrupted, input video frames",
        "- `ground_truth.json` -- hand-written labels: the gestures actually made,",
        "  when, and what the model was expected to say",
        "- `score.json` -- this session scored against those labels",
    ]
    if summary["audio_included"]:
        lines.append(
            "- `input_audio.wav` / `output_audio.wav` -- 16 kHz mono in (as granted"
            " by the browser), 24 kHz mono out"
        )
    else:
        lines.append("- audio: **excluded from this bundle**")
    if summary["frames_included"]:
        lines.append("- `frames/*.jpg` -- the JPEG frames as sent, 1 FPS")
    else:
        lines.append("- video frames: **excluded from this bundle**")

    lines += [
        "",
        "Top level: `report.json` (all scores) and this file.",
        "",
        "## Task and scoring",
        "",
        "The application is a gesture scanner: the model watches a 1 FPS video",
        "stream and calls `report_digit(count=N)` when it sees N fingers held up.",
        "Ground truth is therefore easy to state exactly -- the gesture made, and",
        "when. A detection counts if it names the right N within the labelled",
        "tolerance; anything else is scored as missed or spurious. Latency is",
        "reported per match.",
        "",
        "Function calls are recorded **before** the application's own duplicate",
        "suppression, so the model's raw behaviour is what gets scored.",
        "",
        f"Library versions: {', '.join(f'{k}={v}' for k, v in sorted(versions.items())) or 'unknown'}",
    ]

    flagged = [r for r in reports if r["anomalies"]]
    if flagged:
        lines += [
            "",
            "## Anomalies (please look at these)",
            "",
            "Detected automatically by `evals/score_session.py`.",
            "",
        ]
        for report in flagged:
            lines.append(f"### {Path(report['session_dir']).name}")
            lines.append("")
            for anomaly in report["anomalies"]:
                at = anomaly.get("t_ms")
                where = f" at {at} ms" if at is not None else ""
                lines.append(
                    f"- **{anomaly['type']}**{where}: {anomaly.get('detail', '')}"
                )
                if anomaly.get("excerpt"):
                    lines.append(f"  - excerpt: `{anomaly['excerpt']}`")
            lines.append("")
        lines += [
            "`markdown_in_audio_transcript` is the one worth attention: an output",
            "*audio* transcript containing markdown headings or bullets is not a",
            "transcription of speech. Where it appears together with",
            "`midword_splice`, the transcript stream carried content from another",
            "generation. The application requests AUDIO only and never sends",
            "markdown, so neither can originate on this side.",
        ]

    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("dirs", nargs="*", type=Path, help="session directories")
    parser.add_argument("--all", type=Path, metavar="CAPTURE_DIR")
    parser.add_argument("-o", "--out", type=Path, help="output .tar.gz")
    parser.add_argument(
        "--no-audio", action="store_true", help="omit microphone and model audio"
    )
    parser.add_argument("--no-frames", action="store_true", help="omit camera frames")
    args = parser.parse_args(argv)

    session_dirs = list(args.dirs)
    if args.all:
        session_dirs += sorted(
            p.parent for p in args.all.glob("*/events.jsonl") if p.is_file()
        )
    if not session_dirs:
        parser.error("no sessions given (pass directories or --all CAPTURE_DIR)")

    out = args.out or Path(f"eap-submission-{time.strftime('%Y%m%dT%H%M%S')}.tar.gz")
    summary = build(
        session_dirs,
        out,
        include_audio=not args.no_audio,
        include_frames=not args.no_frames,
    )

    print(json.dumps(summary, indent=2))
    unlabelled = summary["sessions"] - summary["labelled"]
    if unlabelled:
        print(
            f"\nWarning: {unlabelled} session(s) have no ground truth and are"
            " included unscored.",
            file=sys.stderr,
        )
    print(f"\nUpload this file: {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
