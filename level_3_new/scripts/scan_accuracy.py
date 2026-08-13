#!/usr/bin/env python3
"""Non-interactive scan-accuracy harness for the Live API backend.

Drives the real WebSocket endpoint with fixture hand images instead of a webcam
and a text "scan" stimulus instead of a microphone, then scores what the model
did against known ground truth, with bandwidth and latency accounting for every
run. **Billed**: one real Live session per condition.

    ./scripts/scan_accuracy.py                       # baseline, sharp and quiet
    ./scripts/scan_accuracy.py --lighting dark       # one degraded condition
    ./scripts/scan_accuracy.py --noise chatter       # audio in the room
    ./scripts/scan_accuracy.py --matrix --json out.json    # the whole sweep

Why this exists: `make test` stubs `runner.run_live()`, so it cannot see model
behaviour at all. This measures the thing the suite structurally cannot -- and
it has already overturned one confident diagnosis (see docs/testing-strategy.md
Tier 6).

Scoring reads the raw ADK `functionCall`, not the backend's `match` frame: the
match channel is deduped by design (main.py), and a harness needs to see every
call the model actually made, including the repeats the dedup hides.
"""

import argparse
import asyncio
import contextlib
import io
import json
import math
import pathlib
import random
import struct
import sys
import time
import uuid

import websockets
from PIL import Image, ImageEnhance, ImageFilter

ROOT = pathlib.Path(__file__).resolve().parent.parent
FIXTURES = ROOT / "tests" / "fixtures" / "hands"
CHATTER_PCM = ROOT / "mock" / "mock_audio.pcm"
REFUSAL_MARKERS = ("stabilize", "inadequate", "lighting", "cannot", "unclear")

# Audio framing copied from the browser: the AudioWorklet hands over 128-sample
# blocks at 16 kHz, which is the ~125 packets/sec seen in real session logs.
SAMPLE_RATE = 16000
CHUNK_SAMPLES = 128
CHUNK_SECONDS = CHUNK_SAMPLES / SAMPLE_RATE

# The sweep. Each entry is one Live session; keep them one-variable-apart so a
# difference points at something.
MATRIX = [
    {"label": "baseline", "lighting": "none", "blur_prob": 0.0, "noise": "none"},
    {"label": "motion blur", "lighting": "none", "blur_prob": 0.6, "noise": "none"},
    {"label": "dim light", "lighting": "dim", "blur_prob": 0.0, "noise": "none"},
    {"label": "very dark", "lighting": "dark", "blur_prob": 0.0, "noise": "none"},
    {"label": "backlit", "lighting": "backlit", "blur_prob": 0.0, "noise": "none"},
    {"label": "overexposed", "lighting": "washed", "blur_prob": 0.0, "noise": "none"},
    {"label": "room hiss", "lighting": "none", "blur_prob": 0.0, "noise": "hiss"},
    {
        "label": "background chatter",
        "lighting": "none",
        "blur_prob": 0.0,
        "noise": "chatter",
    },
    {"label": "worst case", "lighting": "dim", "blur_prob": 0.6, "noise": "chatter"},
]


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    p.add_argument("--url", default="ws://127.0.0.1:8080", help="backend base URL")
    p.add_argument(
        "--digits", default="1,2,3,4,5", help="comma-separated digits to test"
    )
    p.add_argument("--rounds", type=int, default=1, help="passes over the digit list")
    p.add_argument(
        "--hold", type=float, default=9.0, help="seconds to hold each hand up"
    )
    p.add_argument(
        "--stimulus-delay",
        type=float,
        default=4.0,
        help=(
            "seconds of frames before saying 'scan'. Not a tuning knob: the model "
            "answers roughly a second after the stimulus using the video it has "
            "already ingested, so asking immediately gets the PREVIOUS hand "
            "counted. At 0.5s every trial reported its predecessor's digit."
        ),
    )
    p.add_argument(
        "--lighting",
        default="none",
        choices=("none", "dim", "dark", "backlit", "washed"),
        help="degrade the fixtures the way a real room would",
    )
    p.add_argument(
        "--noise",
        default="none",
        choices=("none", "hiss", "chatter"),
        help="stream audio alongside the video: hiss = room tone, chatter = speech",
    )
    p.add_argument(
        "--noise-level", type=float, default=0.08, help="noise amplitude, 0-1"
    )
    p.add_argument(
        "--blur-prob", type=float, default=0.0, help="fraction of frames to blur"
    )
    p.add_argument(
        "--blur-radius", type=float, default=3.0, help="Gaussian radius when blurred"
    )
    p.add_argument("--jitter", type=int, default=0, help="max pixel shift per frame")
    p.add_argument(
        "--jpeg-quality",
        type=int,
        default=60,
        help="JPEG quality per frame; 60 is what useGeminiSocket.js sends",
    )
    p.add_argument(
        "--scale",
        type=float,
        default=1.0,
        help="scale frames from 640x480 before encoding; 0.5 quarters the pixels",
    )
    p.add_argument(
        "--seed", type=int, default=1, help="RNG seed, so runs are reproducible"
    )
    p.add_argument(
        "--min-rate", type=float, default=0.0, help="exit non-zero below this hit rate"
    )
    p.add_argument(
        "--matrix", action="store_true", help="run every condition in MATRIX"
    )
    p.add_argument(
        "--label", default="custom", help="name for this condition in the report"
    )
    p.add_argument("--json", dest="json_out", help="write full results here")
    return p.parse_args()


def load_fixtures(digits: list[int]) -> dict[int, Image.Image]:
    images = {}
    for d in digits:
        path = FIXTURES / f"hand_{d}.jpg"
        if not path.exists():
            sys.exit(f"missing fixture {path} -- see {FIXTURES}/README.md")
        images[d] = Image.open(path).convert("RGB")
    return images


def apply_lighting(img: Image.Image, mode: str) -> Image.Image:
    """Degrade a fixture the way a real room does.

    Deterministic PIL transforms rather than regenerated images on purpose: the
    ground truth has to survive the degradation. Darkening a verified 3-finger
    hand still shows three fingers; regenerating "a hand in dim light" gives an
    image whose count nobody has checked.
    """
    if mode == "none":
        return img
    if mode == "dim":
        return ImageEnhance.Brightness(img).enhance(0.35)
    if mode == "dark":
        return ImageEnhance.Contrast(
            ImageEnhance.Brightness(img).enhance(0.15)
        ).enhance(0.8)
    if mode == "washed":
        return ImageEnhance.Contrast(
            ImageEnhance.Brightness(img).enhance(1.75)
        ).enhance(0.45)
    if mode == "backlit":
        # Bright window behind the subject: the hand falls into silhouette while
        # the top of the frame blows out.
        base = ImageEnhance.Brightness(img).enhance(0.45)
        glare = Image.new("L", base.size)
        for y in range(base.size[1]):
            value = int(255 * max(0.0, 1.0 - (y / base.size[1]) * 1.6))
            for x in range(base.size[0]):
                glare.putpixel((x, y), value)
        white = Image.new("RGB", base.size, (255, 255, 255))
        return Image.composite(white, base, glare.point(lambda v: int(v * 0.65)))
    return img


def encode_frame(
    img: Image.Image, rng: random.Random, args: argparse.Namespace
) -> bytes:
    """One JPEG frame, matching what useGeminiSocket.js sends (640x480, q60)."""
    frame = img
    if args.jitter:
        dx = rng.randint(-args.jitter, args.jitter)
        dy = rng.randint(-args.jitter, args.jitter)
        frame = frame.transform(frame.size, Image.AFFINE, (1, 0, dx, 0, 1, dy))
    if args.blur_prob and rng.random() < args.blur_prob:
        frame = frame.filter(ImageFilter.GaussianBlur(args.blur_radius))
    if args.scale != 1.0:
        frame = frame.resize(
            (int(frame.width * args.scale), int(frame.height * args.scale)),
            Image.LANCZOS,
        )
    buf = io.BytesIO()
    frame.save(buf, "JPEG", quality=args.jpeg_quality)
    return buf.getvalue()


def load_chatter() -> bytes:
    """Speech-shaped background audio, resampled 24 kHz -> 16 kHz.

    mock_audio.pcm is recorded model output, which makes it the only real speech
    in the repo. It is not the user talking to the scanner -- it is a voice in
    the room, which is exactly the condition being tested.
    """
    if not CHATTER_PCM.exists():
        sys.exit(f"missing {CHATTER_PCM}; use --noise hiss instead")
    raw = CHATTER_PCM.read_bytes()
    src = struct.unpack(f"<{len(raw) // 2}h", raw[: len(raw) // 2 * 2])
    ratio = 24000 / SAMPLE_RATE
    out = [src[min(int(i * ratio), len(src) - 1)] for i in range(int(len(src) / ratio))]
    return struct.pack(f"<{len(out)}h", *out)


def noise_source(mode: str, level: float, rng: random.Random) -> bytes:
    if mode == "chatter":
        pcm = load_chatter()
        peak = max(abs(s) for s in struct.unpack(f"<{len(pcm) // 2}h", pcm)) or 1
        gain = (level * 32767) / peak
        samples = struct.unpack(f"<{len(pcm) // 2}h", pcm)
        return struct.pack(f"<{len(samples)}h", *(int(s * gain) for s in samples))
    # Room tone: white noise shaped by a slow drift so it is not perfectly flat.
    count = SAMPLE_RATE * 10
    amp = level * 32767
    return struct.pack(
        f"<{count}h",
        *(
            int(amp * rng.uniform(-1, 1) * (0.7 + 0.3 * math.sin(i / 4000)))
            for i in range(count)
        ),
    )


def classify(events: list[dict], expected: int) -> dict:
    calls = [e for e in events if e["kind"] == "call"]
    speech = " ".join(e["text"] for e in events if e["kind"] == "speech").lower()
    if calls:
        first = calls[0]
        return {
            "outcome": "hit" if first["count"] == expected else "wrong",
            "reported": first["count"],
            "latency": round(first["t"], 2),
            "calls": len(calls),
            "speech": speech.strip(),
        }
    outcome = "refused" if any(m in speech for m in REFUSAL_MARKERS) else "silent"
    return {
        "outcome": outcome,
        "reported": None,
        "latency": None,
        "calls": 0,
        "speech": speech.strip(),
    }


async def run_condition(args: argparse.Namespace, cond: dict) -> dict:
    """One Live session under one condition. Returns scored trials + counters."""
    for key in ("lighting", "blur_prob", "noise"):
        setattr(args, key, cond[key])
    label = cond["label"]

    digits = [int(d) for d in args.digits.split(",")]
    rng = random.Random(args.seed)
    images = {
        d: apply_lighting(img, args.lighting)
        for d, img in load_fixtures(digits).items()
    }

    url = f"{args.url.rstrip('/')}/ws/scan-accuracy/{uuid.uuid4()}"
    received: list[dict] = []
    counters = {"video_bytes": 0, "video_frames": 0, "audio_bytes": 0, "down_bytes": 0}

    async with websockets.connect(url, max_size=None) as ws:
        config = json.loads(await ws.recv())
        if config.get("type") != "config":
            raise RuntimeError(f"expected config frame, got {config.get('type')!r}")
        jpeg_prefix = config.get("jpeg_prefix", 2)
        audio_prefix = config.get("audio_prefix", 1)
        interval = config.get("frame_interval_ms", 1000) / 1000.0
        fps = config.get("video_fps")

        async def receiver() -> None:
            async for raw in ws:
                now = time.monotonic()
                counters["down_bytes"] += len(raw)
                if isinstance(raw, bytes):
                    continue
                msg = json.loads(raw)
                for part in (msg.get("content") or {}).get("parts") or []:
                    fc = part.get("functionCall")
                    if fc and fc.get("name") == "report_digit":
                        argv = fc.get("args") or {}
                        received.append(
                            {
                                "kind": "call",
                                "t": now,
                                "count": argv.get("count", argv.get("digit")),
                            }
                        )
                tr = msg.get("outputTranscription")
                if tr and tr.get("finished") and tr.get("text"):
                    received.append({"kind": "speech", "t": now, "text": tr["text"]})

        async def audio_streamer() -> None:
            """Stream room audio for the whole session, paced like the browser."""
            if args.noise == "none":
                return
            pcm = noise_source(args.noise, args.noise_level, random.Random(args.seed))
            await ws.send(
                json.dumps({"type": "audio_config", "sample_rate": SAMPLE_RATE})
            )
            offset = 0
            step = CHUNK_SAMPLES * 2
            while True:
                if offset + step > len(pcm):
                    offset = 0
                chunk = pcm[offset : offset + step]
                offset += step
                await ws.send(bytes([audio_prefix]) + chunk)
                counters["audio_bytes"] += len(chunk) + 1
                await asyncio.sleep(CHUNK_SECONDS)

        recv_task = asyncio.create_task(receiver())
        audio_task = asyncio.create_task(audio_streamer())

        # Let the opening turn ("Scanner Online.") land, or its speech is scored
        # against the first digit.
        await asyncio.sleep(4.0)
        received.clear()

        started = time.monotonic()
        results = []
        for rnd in range(args.rounds):
            for digit in digits:
                start = time.monotonic()
                deadline = start + args.hold
                stimulus_at = None
                frames = 0
                while time.monotonic() < deadline:
                    payload = encode_frame(images[digit], rng, args)
                    await ws.send(bytes([jpeg_prefix]) + payload)
                    counters["video_bytes"] += len(payload) + 1
                    counters["video_frames"] += 1
                    frames += 1
                    if (
                        stimulus_at is None
                        and time.monotonic() - start >= args.stimulus_delay
                    ):
                        await ws.send(json.dumps({"type": "text", "text": "scan"}))
                        stimulus_at = time.monotonic()
                    await asyncio.sleep(interval)

                # Scored from the stimulus, not the window start: anything said
                # before it was asked belongs to the previous trial.
                origin = stimulus_at or start
                window = [
                    {**e, "t": e["t"] - origin}
                    for e in received
                    if origin <= e["t"] <= deadline
                ]
                scored = classify(window, digit) | {
                    "condition": label,
                    "round": rnd + 1,
                    "expected": digit,
                    "frames": frames,
                }
                results.append(scored)
                mark = {
                    "hit": "OK",
                    "wrong": "WRONG",
                    "refused": "REFUSED",
                    "silent": "SILENT",
                }[scored["outcome"]]
                latency = (
                    f"{scored['latency']}s" if scored["latency"] is not None else "--"
                )
                print(
                    f"  {label:<19} digit {digit}: {mark:<8} reported={scored['reported']} "
                    f'latency={latency:<6} speech="{scored["speech"][:44]}"'
                )

        elapsed = time.monotonic() - started
        for task in (recv_task, audio_task):
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

    hits = sum(r["outcome"] == "hit" for r in results)
    latencies = sorted(r["latency"] for r in results if r["latency"] is not None)
    return {
        "label": label,
        "fps": fps,
        "seconds": round(elapsed, 1),
        "trials": len(results),
        "hits": hits,
        "wrong": sum(r["outcome"] == "wrong" for r in results),
        "refused": sum(r["outcome"] == "refused" for r in results),
        "silent": sum(r["outcome"] == "silent" for r in results),
        "p50": latencies[len(latencies) // 2] if latencies else None,
        "p90": latencies[min(len(latencies) - 1, int(len(latencies) * 0.9))]
        if latencies
        else None,
        "up_kbps": round(
            (counters["video_bytes"] + counters["audio_bytes"]) * 8 / elapsed / 1000, 1
        ),
        "video_kbps": round(counters["video_bytes"] * 8 / elapsed / 1000, 1),
        "audio_kbps": round(counters["audio_bytes"] * 8 / elapsed / 1000, 1),
        "down_kbps": round(counters["down_bytes"] * 8 / elapsed / 1000, 1),
        "avg_frame_bytes": round(
            counters["video_bytes"] / max(counters["video_frames"], 1)
        ),
        "results": results,
    }


def render(summaries: list[dict]) -> None:
    """One table. Accuracy, latency and bandwidth for every condition."""
    print(
        f"\n{'condition':<20} {'hits':>7}  {'p50':>6} {'p90':>6}  {'up':>9} {'down':>9}  outcome"
    )
    print("-" * 86)
    for s in summaries:
        rate = s["hits"] / s["trials"] if s["trials"] else 0
        bar = "#" * round(rate * 10) + "." * (10 - round(rate * 10))
        p50 = f"{s['p50']:.2f}s" if s["p50"] is not None else "   --"
        p90 = f"{s['p90']:.2f}s" if s["p90"] is not None else "   --"
        detail = []
        for key in ("wrong", "refused", "silent"):
            if s[key]:
                detail.append(f"{s[key]} {key}")
        print(
            f"{s['label']:<20} {s['hits']}/{s['trials']} {bar}  {p50:>6} {p90:>6}  "
            f"{s['up_kbps']:>6.1f}kb {s['down_kbps']:>6.1f}kb  {', '.join(detail) or 'clean'}"
        )
    total_up = sum(s["up_kbps"] for s in summaries) / len(summaries)
    total_down = sum(s["down_kbps"] for s in summaries) / len(summaries)
    frame = summaries[0]["avg_frame_bytes"]
    print(
        f"\nmean uplink {total_up:.1f} kbit/s, downlink {total_down:.1f} kbit/s; "
        f"video frame {frame} bytes at {summaries[0]['fps']} FPS"
    )


async def main() -> int:
    args = parse_args()
    conditions = (
        MATRIX
        if args.matrix
        else [
            {
                "label": args.label,
                "lighting": args.lighting,
                "blur_prob": args.blur_prob,
                "noise": args.noise,
            }
        ]
    )

    summaries = []
    for cond in conditions:
        summaries.append(await run_condition(args, cond))

    render(summaries)

    if args.json_out:
        pathlib.Path(args.json_out).write_text(json.dumps(summaries, indent=2))
        print(f"wrote {args.json_out}")

    trials = sum(s["trials"] for s in summaries)
    hits = sum(s["hits"] for s in summaries)
    rate = hits / trials if trials else 0.0
    if args.min_rate and rate < args.min_rate:
        print(
            f"FAIL: hit rate {rate:.0%} below --min-rate {args.min_rate:.0%}",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
