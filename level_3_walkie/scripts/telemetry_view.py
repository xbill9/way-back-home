#!/usr/bin/env python3
"""Render a recorded browser session as a standalone HTML timeline.

    # in the app: expand the Trace panel, press "save"
    ./scripts/telemetry_view.py ~/Downloads/telemetry-2026-08-13T14-23-01.json out.html

The live panels only ever hold the last 40 seconds, which is no help for
"why did that run feel bad?" -- the question every session this project has
debugged started with. This renders the whole run: bandwidth split into video
and microphone, the two latencies, capture rate, token growth, and the event
trace, all on one shared time axis so a spike can be lined up against what was
happening when it spiked.

Free and offline: it reads a file the browser wrote, and talks to nothing.

Styling is imported from telemetry_report.py rather than copied, so the two
outputs stay one design. Charts are hand-built inline SVG, no dependencies.
"""

import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from telemetry_report import CSS, esc

# Same categorical trio as the sweep report, validated in both themes. Latency
# and capture rate are single-series and use the accent instead.
SERIES = {
    "video": "var(--cat-video)",
    "audio": "var(--cat-audio)",
    "down": "var(--cat-down)",
}


def fmt_clock(ms: int) -> str:
    seconds = ms // 1000
    return f"{seconds // 60}:{seconds % 60:02d}"


def fmt_tokens(n) -> str:
    n = n or 0
    return f"{n / 1000:.1f}k" if n >= 1000 else str(n)


def area_chart(samples, keys, height=90, label="") -> str:
    """Stacked area over time. One point per sample, x is elapsed time."""
    if len(samples) < 2:
        return "<p class='sub'>Not enough samples to plot.</p>"
    width = 860
    span = max(samples[-1]["_elapsed"], 1)
    peak = max((sum(s.get(k) or 0 for k in keys) for s in samples), default=1) or 1

    def x(s):
        return (s["_elapsed"] / span) * width

    parts = []
    # Drawn back to front so the stack reads bottom-up in the legend order.
    running = {id(s): 0.0 for s in samples}
    for key in keys:
        pts_top, pts_bottom = [], []
        for s in samples:
            base = running[id(s)]
            value = s.get(key) or 0
            running[id(s)] = base + value
            y_top = height - ((base + value) / peak) * height
            y_bot = height - (base / peak) * height
            pts_top.append(f"{x(s):.1f},{y_top:.1f}")
            pts_bottom.append(f"{x(s):.1f},{y_bot:.1f}")
        poly = " ".join(pts_top + list(reversed(pts_bottom)))
        parts.append(f'<polygon points="{poly}" fill="{SERIES[key]}" opacity="0.75"/>')

    return (
        f'<svg viewBox="0 0 {width} {height}" width="100%" height="{height}" '
        f'preserveAspectRatio="none" role="img" aria-label="{esc(label)}">'
        f"{''.join(parts)}</svg>"
    )


def line_chart(samples, key, height=90, label="", unit="") -> str:
    """Single measure over time, gaps where it was never measured."""
    points = [(s["_elapsed"], s.get(key)) for s in samples if s.get(key) is not None]
    if len(points) < 2:
        return "<p class='sub'>Never measured during this session.</p>"
    width = 860
    span = max(samples[-1]["_elapsed"], 1)
    peak = max(v for _, v in points) or 1
    coords = " ".join(
        f"{(t / span) * width:.1f},{height - (v / peak) * height:.1f}"
        for t, v in points
    )
    worst_t, worst_v = max(points, key=lambda p: p[1])
    return (
        f'<svg viewBox="0 0 {width} {height}" width="100%" height="{height}" '
        f'preserveAspectRatio="none" role="img" aria-label="{esc(label)}">'
        f'<polyline points="{coords}" fill="none" stroke="var(--accent)" stroke-width="2" '
        f'stroke-linejoin="round" vector-effect="non-scaling-stroke"/>'
        f'<circle cx="{(worst_t / span) * width:.1f}" cy="{height - (worst_v / peak) * height:.1f}" '
        f'r="4" fill="var(--accent)"><title>worst: {worst_v}{esc(unit)} at '
        f"{fmt_clock(worst_t)}</title></circle>"
        f"</svg>"
    )


def event_rows(events, started_at) -> str:
    rows = []
    for e in events:
        rows.append(
            f"<tr><td class='num'>{fmt_clock(e['t'] - started_at)}</td>"
            f"<td>{esc(e['kind'])}</td><td>{esc(e['text'])}</td></tr>"
        )
    if not rows:
        rows.append("<tr><td colspan='3'>No events recorded.</td></tr>")
    return (
        "<div class='scroll'><table><thead><tr><th>At</th><th>Kind</th><th>Detail</th>"
        f"</tr></thead><tbody>{''.join(rows)}</tbody></table></div>"
    )


def build(session: dict) -> str:
    started = session.get("started_at") or 0
    samples = session.get("samples") or []
    for s in samples:
        s["_elapsed"] = s["t"] - started
    events = session.get("events") or []
    config = session.get("config") or {}
    duration = (session.get("ended_at", started) - started) or 1

    peak_up = max((s.get("up") or 0 for s in samples), default=0)
    mean_up = sum(s.get("up") or 0 for s in samples) / len(samples) if samples else 0
    mean_audio = (
        sum(s.get("audio") or 0 for s in samples) / len(samples) if samples else 0
    )
    audio_share = (mean_audio / mean_up * 100) if mean_up else 0
    detects = [s["detectMs"] for s in samples if s.get("detectMs") is not None]
    worst_detect = max(detects, default=None)
    tokens = max((s.get("contextTokens") or 0 for s in samples), default=0)
    out_tokens = max((s.get("outputTokens") or 0 for s in samples), default=0)
    megabytes = mean_up * (duration / 1000) / 8 / 1000

    capture = (
        f"{config.get('video_width')}x{config.get('video_height')} "
        f"q{config.get('jpeg_quality')} at {config.get('video_fps')} FPS"
        if config
        else "capture settings not recorded"
    )

    return f"""<title>Session Telemetry</title>
<style>{CSS}</style>
<div class="wrap">
  <header>
    <div class="eyebrow">Recorded session</div>
    <h1>{fmt_clock(duration)} of scanning, {len(samples)} samples, {len(events)} events</h1>
    <p class="sub mono">{esc(capture)}</p>
  </header>

  <section>
    <div class="tiles">
      <div class="tile"><span class="k">Uplink, mean</span>
        <span class="n">{mean_up:.0f}</span><span class="d">kbit/s &middot; peak {peak_up:.0f}</span></div>
      <div class="tile"><span class="k">Microphone share</span>
        <span class="n" style="color:var(--cat-audio)">{audio_share:.0f}%</span>
        <span class="d">of everything sent</span></div>
      <div class="tile"><span class="k">Worst detect</span>
        <span class="n">{f"{worst_detect} ms" if worst_detect is not None else "--"}</span>
        <span class="d">{len(detects)} measured</span></div>
      <div class="tile"><span class="k">Data sent</span>
        <span class="n">{megabytes:.1f}</span><span class="d">MB uplink total</span></div>
    </div>
  </section>

  <section>
    <h2>Bandwidth</h2>
    <figure>
      <div class="legend">
        <span><i class="swatch" style="background:var(--cat-video)"></i>video uplink</span>
        <span><i class="swatch" style="background:var(--cat-audio)"></i>microphone uplink</span>
      </div>
      {area_chart(samples, ["video", "audio"], label="Uplink over the session")}
      <figcaption>Microphone audio averaged {audio_share:.0f}% of the uplink. It is raw
      16&nbsp;kHz 16-bit PCM &mdash; 256&nbsp;kbit/s, uncompressible, and sent continuously
      unless the mic gate is on. Video is one JPEG per frame with no interframe
      compression, so it costs the same whether or not anything moved.</figcaption>
    </figure>
  </section>

  <section>
    <h2>Downlink</h2>
    <figure>
      {area_chart(samples, ["down"], label="Downlink over the session")}
      <figcaption>Model audio and events coming back. A collapse to near zero while the
      session is still running means the model has stopped answering &mdash; the signature
      of continuous room noise holding a turn open.</figcaption>
    </figure>
  </section>

  <section>
    <h2>Detection latency</h2>
    <figure>
      {line_chart(samples, "detectMs", label="Detection latency", unit="ms")}
      <figcaption>Frame sent &rarr; <code>match</code> frame, so it includes the network.
      The marked point is the worst in the run; hover it for the timestamp, then find that
      time in the trace below.</figcaption>
    </figure>
  </section>

  <section>
    <h2>Capture rate</h2>
    <figure>
      {line_chart(samples, "fps", label="Capture rate", unit=" fps")}
      <figcaption>Frames actually sent per second. A sag here without a matching sag in
      anything else usually means the tab was backgrounded.</figcaption>
    </figure>
  </section>

  <section>
    <h2>Context growth</h2>
    <figure>
      {line_chart(samples, "contextTokens", label="Context tokens", unit=" tokens")}
      <figcaption>Reached {fmt_tokens(tokens)} prompt tokens, {fmt_tokens(out_tokens)} out.
      The prompt count is cumulative: the whole context is re-counted every turn, and at one
      frame per second the image share is what grows fastest.</figcaption>
    </figure>
  </section>

  <section>
    <h2>Trace</h2>
    {event_rows(events, started)}
  </section>

  <footer class="mono">recorded in the browser &middot; rendered by scripts/telemetry_view.py</footer>
</div>
"""


def main() -> int:
    if len(sys.argv) < 3:
        print(__doc__.strip())
        return 1
    session = json.loads(pathlib.Path(sys.argv[1]).read_text())
    out = pathlib.Path(sys.argv[2])
    out.write_text(build(session))
    print(f"wrote {out} ({out.stat().st_size} bytes)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
