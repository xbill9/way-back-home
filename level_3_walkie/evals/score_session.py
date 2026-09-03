#!/usr/bin/env python3
"""Score a recorded session against its hand-written ground truth.

Usage:
    python evals/score_session.py <session_dir> [<session_dir> ...]
    python evals/score_session.py --all <capture_dir>
    python evals/score_session.py --all <capture_dir> --json report.json

Reads `events.jsonl` + `ground_truth.json` from a directory written by
`backend/app/eval_capture.py` and reports three things:

  detections   did report_digit fire, with the right count, in time
  utterances   did the model say what it was supposed to (and nothing forbidden)
  anomalies    machine-checkable signs the output stream itself misbehaved

The anomaly checks earn their place. On 2026-08-12 a finished output transcript
on models/walkie-talkie arrived as "Stabilize hand." with an unrelated
markdown-formatted essay spliced into it mid-word. Nothing about digit accuracy
would have caught that, and it is exactly the class of defect the EAP wants
reported, so it is checked here rather than left to whoever reads the log.

Exit status is 0 if every session met its ground truth with no anomalies, 1
otherwise -- so this can gate a bundle before it is uploaded.
"""

from __future__ import annotations

import argparse
import itertools
import json
import re
import sys
from pathlib import Path
from typing import Any

# Markdown structure in an *audio* transcript is the strong signal. An output
# transcript is a transcription of speech: nobody pronounces "###" or "**". Kept
# deliberately narrow -- a stray asterisk in dictated speech should not trip it,
# so each pattern needs markdown *structure*, not just the character.
_MARKDOWN_PATTERNS = (
    (re.compile(r"^\s{0,3}#{1,6}\s+\S", re.MULTILINE), "markdown heading"),
    (re.compile(r"\*\*\S[^*]*\S\*\*"), "markdown bold"),
    (re.compile(r"^\s{0,3}[-*+]\s{2,}\S", re.MULTILINE), "markdown bullet"),
    (
        re.compile(r"^\s{0,3}\d+\.\s+\S.*\n\s{0,3}\d+\.\s+\S", re.MULTILINE),
        "numbered list",
    ),
)

# A chunk that resumes mid-word right after a sentence ended: "hand." + "ged ".
# This is the splice signature itself, checked across consecutive chunks.
_SENTENCE_END = re.compile(r"[.!?]\s*$")
_STARTS_MIDWORD = re.compile(r"^[a-z]{2,}")


def load_events(session_dir: Path) -> list[dict[str, Any]]:
    path = session_dir / "events.jsonl"
    if not path.exists():
        raise FileNotFoundError(f"no events.jsonl in {session_dir}")
    events = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            events.append(json.loads(line))
    return events


def load_ground_truth(session_dir: Path) -> dict[str, Any] | None:
    """The labels, or None if there aren't any yet.

    An untouched template counts as "none": the recorder writes example values,
    and scoring against those reports confident nonsense (the first real run
    came back "0/1 matched, 5 spurious" purely because nobody had edited the
    file). `_template` is removed by hand when the labels are written.
    """
    path = session_dir / "ground_truth.json"
    if not path.exists():
        return None
    gt = json.loads(path.read_text(encoding="utf-8"))
    if gt.get("_template"):
        return None
    return gt


def score_detections(
    events: list[dict[str, Any]], expected: list[dict[str, Any]]
) -> dict[str, Any]:
    """Match each expected gesture to the earliest unused report_digit for it.

    Greedy earliest-match, not best-match: the metric that matters is how fast
    the model reacted, so an expectation should claim the first detection that
    could plausibly be it.
    """
    calls = [
        e
        for e in events
        if e.get("kind") == "function_call" and e.get("name") == "report_digit"
    ]
    for call in calls:
        args = call.get("args") or {}
        call["_count"] = args.get("count", args.get("digit"))

    claimed: set[int] = set()
    matches, misses = [], []

    for want in expected:
        count = want.get("count")
        at_ms = want.get("at_ms", 0)
        tol = want.get("tolerance_ms", 4000)
        hit = None
        for i, call in enumerate(calls):
            if i in claimed or call["_count"] != count:
                continue
            # Only forward in time: a detection before the gesture is not a
            # detection of it.
            delay = call["t_ms"] - at_ms
            if -250 <= delay <= tol:
                hit = (i, call, delay)
                break
        if hit:
            i, call, delay = hit
            claimed.add(i)
            matches.append({"count": count, "at_ms": at_ms, "latency_ms": delay})
        else:
            misses.append({"count": count, "at_ms": at_ms, "tolerance_ms": tol})

    spurious = [
        {"count": c["_count"], "t_ms": c["t_ms"]}
        for i, c in enumerate(calls)
        if i not in claimed
    ]
    latencies = [m["latency_ms"] for m in matches]
    return {
        "expected": len(expected),
        "matched": len(matches),
        "missed": misses,
        "spurious": spurious,
        "matches": matches,
        "latency_ms": {
            "min": min(latencies) if latencies else None,
            "max": max(latencies) if latencies else None,
            "mean": round(sum(latencies) / len(latencies)) if latencies else None,
        },
    }


def output_transcript(events: list[dict[str, Any]]) -> str:
    """The model's finished output transcripts, concatenated."""
    return "\n".join(
        e.get("text", "")
        for e in events
        if e.get("kind") == "output_transcription" and e.get("finished")
    )


def score_utterances(
    events: list[dict[str, Any]], gt: dict[str, Any]
) -> dict[str, Any]:
    transcript = output_transcript(events).lower()
    said = [u for u in gt.get("expected_utterances", []) if u.lower() in transcript]
    missing = [
        u for u in gt.get("expected_utterances", []) if u.lower() not in transcript
    ]
    forbidden = [
        u for u in gt.get("forbidden_utterances", []) if u.lower() in transcript
    ]
    return {"said": said, "missing": missing, "forbidden_present": forbidden}


def find_anomalies(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Machine-checkable defects in the output stream itself."""
    anomalies: list[dict[str, Any]] = []

    chunks = [e for e in events if e.get("kind") == "output_transcription"]

    for e in chunks:
        text = e.get("text") or ""
        for pattern, label in _MARKDOWN_PATTERNS:
            if pattern.search(text):
                anomalies.append(
                    {
                        "type": "markdown_in_audio_transcript",
                        "detail": label,
                        "t_ms": e.get("t_ms"),
                        "finished": e.get("finished"),
                        "excerpt": text[:200],
                    }
                )
                break

    # Mid-word resume across consecutive partial chunks.
    partials = [e for e in chunks if not e.get("finished")]
    for prev, cur in itertools.pairwise(partials):
        prev_text, cur_text = prev.get("text") or "", cur.get("text") or ""
        if _SENTENCE_END.search(prev_text) and _STARTS_MIDWORD.match(cur_text):
            anomalies.append(
                {
                    "type": "midword_splice",
                    "detail": f"{prev_text[-40:]!r} then {cur_text[:40]!r}",
                    "t_ms": cur.get("t_ms"),
                }
            )

    # Identity changing mid-stream is direct evidence of a mixed stream, which
    # is what distinguishes "the model rambled" from "someone else's generation
    # arrived here".
    #
    # DORMANT on ADK 2.6.3, confirmed against a real session on 2026-08-12: all
    # three fields arrive as None. ADK builds each live event as
    # `Event(id=..., invocation_id=..., author=...)` (base_llm_flow.py:911) and
    # never copies them off the LlmResponse, so this check cannot fire no matter
    # what the server sends -- the same class of gap as `interaction_status`.
    # Kept rather than deleted because the recording stores the fields, so it
    # starts working the day ADK populates them; do not read a clean run as
    # evidence the stream was consistent.
    for field in ("live_session_id", "interaction_id", "model_version"):
        seen = {e.get(field) for e in chunks if e.get(field) is not None}
        if len(seen) > 1:
            anomalies.append(
                {
                    "type": "inconsistent_stream_identity",
                    "detail": f"{field} took {len(seen)} values: {sorted(map(str, seen))}",
                }
            )

    return anomalies


def score_session(session_dir: Path) -> dict[str, Any]:
    events = load_events(session_dir)
    gt = load_ground_truth(session_dir)
    manifest_path = session_dir / "manifest.json"
    manifest = (
        json.loads(manifest_path.read_text(encoding="utf-8"))
        if manifest_path.exists()
        else {}
    )

    report: dict[str, Any] = {
        "session_dir": str(session_dir),
        "session_id": manifest.get("session_id"),
        "model_id": manifest.get("model_id"),
        "duration_ms": manifest.get("duration_ms"),
        "labelled": gt is not None,
        "anomalies": find_anomalies(events),
    }

    if gt is None:
        report["note"] = "no ground_truth.json -- anomaly checks only"
    else:
        report["detections"] = score_detections(events, gt.get("expected_digits", []))
        report["utterances"] = score_utterances(events, gt)
        report["notes"] = gt.get("notes", "")

    det = report.get("detections")
    utt = report.get("utterances")
    report["passed"] = (
        not report["anomalies"]
        and (det is None or (det["matched"] == det["expected"] and not det["spurious"]))
        and (utt is None or (not utt["missing"] and not utt["forbidden_present"]))
    )
    return report


def print_report(report: dict[str, Any]) -> None:
    name = Path(report["session_dir"]).name
    status = "PASS" if report["passed"] else "FAIL"
    print(f"\n{status}  {name}  ({report.get('model_id')})")

    if not report["labelled"]:
        print(
            "  ground truth: NOT FILLED IN -- edit ground_truth.json and delete"
            ' its "_template" line to score this session'
        )

    det = report.get("detections")
    if det:
        print(
            f"  detections:   {det['matched']}/{det['expected']} matched, "
            f"{len(det['spurious'])} spurious"
        )
        if det["latency_ms"]["mean"] is not None:
            lat = det["latency_ms"]
            print(
                f"  latency:      mean {lat['mean']} ms "
                f"(min {lat['min']}, max {lat['max']})"
            )
        for miss in det["missed"]:
            print(f"    MISSED  count={miss['count']} at {miss['at_ms']} ms")
        for extra in det["spurious"]:
            print(f"    SPURIOUS count={extra['count']} at {extra['t_ms']} ms")

    utt = report.get("utterances")
    if utt:
        for missing in utt["missing"]:
            print(f"    NOT SAID {missing!r}")
        for bad in utt["forbidden_present"]:
            print(f"    FORBIDDEN {bad!r}")

    for anomaly in report["anomalies"]:
        detail = anomaly.get("detail", "")
        at = anomaly.get("t_ms")
        where = f" at {at} ms" if at is not None else ""
        print(f"    ANOMALY {anomaly['type']}{where}: {detail}")
        if anomaly.get("excerpt"):
            print(f"      excerpt: {anomaly['excerpt']!r}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("dirs", nargs="*", type=Path, help="session directories")
    parser.add_argument(
        "--all", type=Path, metavar="CAPTURE_DIR", help="score every session under here"
    )
    parser.add_argument("--json", type=Path, help="also write the full report here")
    args = parser.parse_args(argv)

    session_dirs = list(args.dirs)
    if args.all:
        session_dirs += sorted(
            p.parent for p in args.all.glob("*/events.jsonl") if p.is_file()
        )
    if not session_dirs:
        parser.error("no sessions given (pass directories or --all CAPTURE_DIR)")

    reports = [score_session(d) for d in session_dirs]
    for report in reports:
        print_report(report)

    passed = sum(1 for r in reports if r["passed"])
    print(f"\n{passed}/{len(reports)} sessions passed")

    if args.json:
        args.json.write_text(json.dumps(reports, indent=2) + "\n", encoding="utf-8")
        print(f"report written: {args.json}")

    return 0 if passed == len(reports) else 1


if __name__ == "__main__":
    sys.exit(main())
