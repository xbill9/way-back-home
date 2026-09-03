#!/usr/bin/env python3
"""Render a scan_accuracy.py result set as a standalone HTML report.

    ./scripts/scan_accuracy.py --matrix --json out.json
    ./scripts/telemetry_report.py out.json report.html

Self-contained: no scripts, no fonts, no external requests, so it opens from
disk and survives a strict CSP. Charts are hand-built inline SVG.

Design notes, so the next person changing this knows what the rules were:

* Conditions are grouped visual / audio because that split *is* the finding. It
  is structure carrying information, not decoration.
* Accuracy uses status colour (good/warn/bad), which is state. Bandwidth uses
  the categorical trio, which is identity. The two sets never mix.
* Both palettes were checked with the dataviz validator in light AND dark:
  light  #1E6FA8 #B85C00 #7A4B9E
  dark   #3E8FCB #C67F22 #9A70BC
  Green+orange was the first attempt and FAILED protan separation (dE 2.9).
* Latency is a range mark (p50 dot on a p50-p90 line), not two bar series, so
  it needs no second categorical hue.
* No gauges anywhere: bandwidth has no natural maximum, and a filled meter
  would have to invent a ceiling.
"""

import html
import json
import pathlib
import sys

AUDIO_CONDITIONS = {"room hiss", "background chatter", "worst case"}

CSS = """
:root {
  --paper: #eef1f3; --surface: #fbfcfd; --ink: #121a21; --muted: #55646f;
  --rule: #d5dde2; --rule-soft: #e6ecef;
  --accent: #1e6fa8; --cat-video: #1e6fa8; --cat-audio: #b85c00; --cat-down: #7a4b9e;
  --good: #1b7a46; --warn: #8a6000; --bad: #a8261c;
  --good-fill: #1b7a46; --warn-fill: #b8860b; --bad-fill: #a8261c;
}
@media (prefers-color-scheme: dark) {
  :root:not([data-theme="light"]) {
    --paper: #0e1418; --surface: #161d23; --ink: #e6edf2; --muted: #93a2ae;
    --rule: #26313a; --rule-soft: #1d262d;
    --accent: #3e8fcb; --cat-video: #3e8fcb; --cat-audio: #c67f22; --cat-down: #9a70bc;
    --good: #4fbf85; --warn: #d6a429; --bad: #e8695c;
    --good-fill: #3fa870; --warn-fill: #c2941f; --bad-fill: #c9503f;
  }
}
:root[data-theme="dark"] {
  --paper: #0e1418; --surface: #161d23; --ink: #e6edf2; --muted: #93a2ae;
  --rule: #26313a; --rule-soft: #1d262d;
  --accent: #3e8fcb; --cat-video: #3e8fcb; --cat-audio: #c67f22; --cat-down: #9a70bc;
  --good: #4fbf85; --warn: #d6a429; --bad: #e8695c;
  --good-fill: #3fa870; --warn-fill: #c2941f; --bad-fill: #c9503f;
}

* { box-sizing: border-box; }
body {
  background: var(--paper); color: var(--ink); margin: 0;
  font: 16px/1.6 ui-sans-serif, system-ui, -apple-system, "Segoe UI", Roboto, sans-serif;
  padding: 40px 24px 72px;
}
.wrap { max-width: 940px; margin: 0 auto; display: flex; flex-direction: column; gap: 34px; }
.mono { font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; }
h1, h2 { text-wrap: balance; margin: 0; letter-spacing: -0.015em; }
h1 { font-size: 30px; line-height: 1.25; }
h2 { font-size: 17px; text-transform: uppercase; letter-spacing: 0.09em;
     font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
     color: var(--muted); font-weight: 600; }
p { margin: 0; max-width: 68ch; }
.lede { font-size: 18px; color: var(--ink); }
.sub { color: var(--muted); font-size: 14px; }
header { display: flex; flex-direction: column; gap: 12px;
         border-bottom: 2px solid var(--rule); padding-bottom: 22px; }
.eyebrow { font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
           font-size: 12px; letter-spacing: 0.22em; text-transform: uppercase; color: var(--accent); }
section { display: flex; flex-direction: column; gap: 14px; }
.tiles { display: grid; grid-template-columns: repeat(auto-fit, minmax(200px, 1fr)); gap: 14px; }
.tile { background: var(--surface); border: 1px solid var(--rule); padding: 16px 18px;
        display: flex; flex-direction: column; gap: 4px; }
.tile .n { font-size: 30px; font-variant-numeric: tabular-nums; line-height: 1.1;
           font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; }
.tile .k { font-size: 11px; text-transform: uppercase; letter-spacing: 0.12em; color: var(--muted); }
.tile .d { font-size: 13px; color: var(--muted); }
figure { margin: 0; background: var(--surface); border: 1px solid var(--rule); padding: 18px; }
figcaption { font-size: 13px; color: var(--muted); margin-top: 12px; max-width: 68ch; }
.scroll { overflow-x: auto; }
svg text { font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; }
.legend { display: flex; flex-wrap: wrap; gap: 16px; font-size: 12px; color: var(--muted);
          font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; margin-bottom: 12px; }
.legend span { display: inline-flex; align-items: center; gap: 6px; }
.swatch { width: 11px; height: 11px; border-radius: 2px; display: inline-block; }
table { border-collapse: collapse; width: 100%; font-size: 13px;
        font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; }
th, td { text-align: left; padding: 7px 10px; border-bottom: 1px solid var(--rule-soft); white-space: nowrap; }
th { font-size: 11px; text-transform: uppercase; letter-spacing: 0.08em; color: var(--muted); font-weight: 600; }
td.num { text-align: right; font-variant-numeric: tabular-nums; }
.pill { display: inline-block; padding: 1px 8px; border-radius: 999px; font-size: 11px;
        letter-spacing: 0.04em; border: 1px solid currentColor; }
.ok { color: var(--good); } .mid { color: var(--warn); } .no { color: var(--bad); }
ul { margin: 0; padding-left: 20px; max-width: 68ch; } li { margin-bottom: 7px; }
footer { border-top: 1px solid var(--rule); padding-top: 18px; color: var(--muted); font-size: 13px; }
"""


def esc(text) -> str:
    return html.escape(str(text))


def status_class(hits: int, trials: int) -> tuple[str, str]:
    if trials and hits == trials:
        return "ok", "var(--good-fill)"
    if hits == 0:
        return "no", "var(--bad-fill)"
    return "mid", "var(--warn-fill)"


def accuracy_chart(rows: list[dict]) -> str:
    """Horizontal hit-rate bars, grouped visual / audio."""
    bar_h, gap, label_w, track_w = 22, 12, 150, 460
    groups = [
        ("Visual conditions", [r for r in rows if r["label"] not in AUDIO_CONDITIONS]),
        ("Audio conditions", [r for r in rows if r["label"] in AUDIO_CONDITIONS]),
    ]
    y, parts = 0, []
    for title, members in groups:
        if not members:
            continue
        parts.append(
            f'<text x="0" y="{y + 11}" font-size="10" letter-spacing="1.6" '
            f'fill="var(--muted)">{esc(title.upper())}</text>'
        )
        y += 24
        for r in members:
            rate = r["hits"] / r["trials"] if r["trials"] else 0
            width = max(rate * track_w, 0)
            _cls, fill = status_class(r["hits"], r["trials"])
            parts.append(
                f'<rect x="{label_w}" y="{y}" width="{track_w}" height="{bar_h}" '
                f'fill="var(--rule-soft)" rx="3"/>'
            )
            if width > 0:
                parts.append(
                    f'<rect x="{label_w}" y="{y}" width="{width:.1f}" height="{bar_h}" '
                    f'fill="{fill}" rx="3"><title>{esc(r["label"])}: '
                    f"{r['hits']} of {r['trials']} correct</title></rect>"
                )
            parts.append(
                f'<text x="{label_w - 10}" y="{y + 15}" font-size="12" text-anchor="end" '
                f'fill="var(--ink)">{esc(r["label"])}</text>'
            )
            detail = []
            for key in ("wrong", "refused", "silent"):
                if r[key]:
                    detail.append(f"{r[key]} {key}")
            parts.append(
                f'<text x="{label_w + track_w + 10}" y="{y + 15}" font-size="12" '
                f'fill="var(--ink)">{r["hits"]}/{r["trials"]}'
                f'<tspan fill="var(--muted)" font-size="11">'
                f"{'  ' + esc(', '.join(detail)) if detail else ''}</tspan></text>"
            )
            y += bar_h + gap
        y += 10
    # Wider than the other charts: the outcome detail ("1 wrong, 1 refused")
    # sits to the right of the count and was being clipped at 760.
    return (
        f'<svg viewBox="0 0 880 {y}" width="880" height="{y}" role="img" '
        f'aria-label="Correct detections by condition">{"".join(parts)}</svg>'
    )


def latency_chart(rows: list[dict]) -> str:
    """p50 dot on a p50-p90 range line. One measure, so no second hue."""
    measured = [r for r in rows if r["p50"] is not None]
    if not measured:
        return "<p class='sub'>No condition produced a measurable latency.</p>"
    peak = max(r["p90"] for r in measured) * 1.15
    row_h, label_w, track_w = 30, 150, 460
    parts = []
    for i, r in enumerate(measured):
        y = i * row_h + 16
        x50 = label_w + (r["p50"] / peak) * track_w
        x90 = label_w + (r["p90"] / peak) * track_w
        parts.append(
            f'<text x="{label_w - 10}" y="{y + 4}" font-size="12" text-anchor="end" '
            f'fill="var(--ink)">{esc(r["label"])}</text>'
        )
        parts.append(
            f'<line x1="{label_w}" y1="{y}" x2="{label_w + track_w}" y2="{y}" '
            f'stroke="var(--rule-soft)" stroke-width="1"/>'
        )
        parts.append(
            f'<line x1="{x50:.1f}" y1="{y}" x2="{x90:.1f}" y2="{y}" stroke="var(--accent)" '
            f'stroke-width="2" stroke-linecap="round" opacity="0.45"/>'
        )
        parts.append(
            f'<circle cx="{x50:.1f}" cy="{y}" r="5" fill="var(--accent)">'
            f"<title>{esc(r['label'])}: median {r['p50']}s, 90th percentile {r['p90']}s</title></circle>"
        )
        parts.append(
            f'<text x="{label_w + track_w + 10}" y="{y + 4}" font-size="12" '
            f'fill="var(--ink)">{r["p50"]:.2f}s'
            f'<tspan fill="var(--muted)" font-size="11">  p90 {r["p90"]:.2f}s</tspan></text>'
        )
    height = len(measured) * row_h + 26
    return (
        f'<svg viewBox="0 0 760 {height}" width="760" height="{height}" role="img" '
        f'aria-label="Response latency by condition, median and 90th percentile">'
        f"{''.join(parts)}</svg>"
    )


def bandwidth_chart(rows: list[dict]) -> str:
    """Stacked uplink (video + audio) over downlink, per condition."""
    peak = max(max(r["up_kbps"], r["down_kbps"]) for r in rows) * 1.08
    label_w, track_w, bar_h, pair_gap, row_gap = 150, 430, 13, 4, 16
    parts, y = [], 8
    for r in rows:
        vid_w = (r["video_kbps"] / peak) * track_w
        aud_w = (r["audio_kbps"] / peak) * track_w
        down_w = (r["down_kbps"] / peak) * track_w
        parts.append(
            f'<text x="{label_w - 10}" y="{y + 18}" font-size="12" text-anchor="end" '
            f'fill="var(--ink)">{esc(r["label"])}</text>'
        )
        parts.append(
            f'<rect x="{label_w}" y="{y}" width="{max(vid_w, 0.5):.1f}" height="{bar_h}" '
            f'fill="var(--cat-video)" rx="3"><title>{esc(r["label"])}: video uplink '
            f"{r['video_kbps']} kbit/s</title></rect>"
        )
        if aud_w > 0:
            # 2px surface gap between stacked segments, per the mark spec.
            parts.append(
                f'<rect x="{label_w + vid_w + 2:.1f}" y="{y}" width="{aud_w:.1f}" height="{bar_h}" '
                f'fill="var(--cat-audio)" rx="3"><title>{esc(r["label"])}: audio uplink '
                f"{r['audio_kbps']} kbit/s</title></rect>"
            )
        parts.append(
            f'<text x="{label_w + max(vid_w + aud_w, 0) + 12:.1f}" y="{y + 11}" font-size="11" '
            f'fill="var(--muted)">{r["up_kbps"]:.0f} up</text>'
        )
        y2 = y + bar_h + pair_gap
        parts.append(
            f'<rect x="{label_w}" y="{y2}" width="{max(down_w, 0.5):.1f}" height="{bar_h}" '
            f'fill="var(--cat-down)" rx="3"><title>{esc(r["label"])}: downlink '
            f"{r['down_kbps']} kbit/s</title></rect>"
        )
        parts.append(
            f'<text x="{label_w + max(down_w, 0) + 12:.1f}" y="{y2 + 11}" font-size="11" '
            f'fill="var(--muted)">{r["down_kbps"]:.0f} down</text>'
        )
        y = y2 + bar_h + row_gap
    return (
        f'<svg viewBox="0 0 760 {y}" width="760" height="{y}" role="img" '
        f'aria-label="Uplink split by video and audio, and downlink, per condition">'
        f"{''.join(parts)}</svg>"
    )


def trial_table(rows: list[dict]) -> str:
    body = []
    for summary in rows:
        for t in summary["results"]:
            cls = {"hit": "ok", "wrong": "mid", "refused": "no", "silent": "no"}[
                t["outcome"]
            ]
            latency = f"{t['latency']:.2f}s" if t["latency"] is not None else "--"
            body.append(
                f"<tr><td>{esc(summary['label'])}</td>"
                f"<td class='num'>{t['expected']}</td>"
                f"<td class='num'>{t['reported'] if t['reported'] is not None else '--'}</td>"
                f"<td><span class='pill {cls}'>{esc(t['outcome'])}</span></td>"
                f"<td class='num'>{latency}</td>"
                f"<td>{esc(t['speech'] or '--')}</td></tr>"
            )
    return (
        "<div class='scroll'><table><thead><tr><th>Condition</th><th>Shown</th>"
        "<th>Reported</th><th>Outcome</th><th>Latency</th><th>Spoken</th></tr></thead>"
        f"<tbody>{''.join(body)}</tbody></table></div>"
    )


def build(rows: list[dict]) -> str:
    trials = sum(r["trials"] for r in rows)
    hits = sum(r["hits"] for r in rows)
    visual = [r for r in rows if r["label"] not in AUDIO_CONDITIONS]
    audio = [r for r in rows if r["label"] in AUDIO_CONDITIONS]
    visual_hits = sum(r["hits"] for r in visual)
    visual_trials = sum(r["trials"] for r in visual)
    audio_hits = sum(r["hits"] for r in audio)
    audio_trials = sum(r["trials"] for r in audio)
    with_audio = [r for r in rows if r["audio_kbps"] > 0]
    audio_share = (
        max(r["audio_kbps"] / r["up_kbps"] for r in with_audio) * 100
        if with_audio
        else 0
    )
    fps = rows[0]["fps"]
    frame = max(r["avg_frame_bytes"] for r in rows)

    return f"""<title>Scanner Condition Sweep</title>
<style>{CSS}</style>
<div class="wrap">
  <header>
    <div class="eyebrow">Gemini Live API &middot; biometric scanner</div>
    <h1>The camera holds up. The microphone is what breaks it.</h1>
    <p class="lede">Nine conditions, one live session each, {trials} scored detections against
    known ground truth. Every visual degradation short of near-black passed. Continuous speech
    in the room stopped the scanner completely.</p>
    <p class="sub mono">{len(rows)} conditions &middot; {fps} FPS &middot; no human in the loop</p>
  </header>

  <section>
    <div class="tiles">
      <div class="tile"><span class="k">Overall</span><span class="n">{hits}/{trials}</span>
        <span class="d">correct detections</span></div>
      <div class="tile"><span class="k">Visual conditions</span>
        <span class="n" style="color:var(--good)">{visual_hits}/{visual_trials}</span>
        <span class="d">blur, dim, backlit, overexposed</span></div>
      <div class="tile"><span class="k">Audio conditions</span>
        <span class="n" style="color:var(--bad)">{audio_hits}/{audio_trials}</span>
        <span class="d">hiss and speech in the room</span></div>
      <div class="tile"><span class="k">Audio share of uplink</span>
        <span class="n" style="color:var(--cat-audio)">{audio_share:.0f}%</span>
        <span class="d">raw PCM, never compressed</span></div>
    </div>
  </section>

  <section>
    <h2>Correct detections</h2>
    <figure>
      <div class="scroll">{accuracy_chart(rows)}</div>
      <figcaption>Grouped by what was degraded, because that split is the finding. "Very dark"
      answering "inadequate lighting." five times is arguably correct behaviour rather than a
      failure &mdash; the frame really is too dark to count, and the model says so in the exact
      words its instruction offers. The audio group is the one to worry about.</figcaption>
    </figure>
  </section>

  <section>
    <h2>Time to answer</h2>
    <figure>
      <div class="scroll">{latency_chart(rows)}</div>
      <figcaption>Measured from the "scan" stimulus to the <code>report_digit</code> call. The dot
      is the median, the line runs to the 90th percentile. Conditions that produced no call at all
      are absent rather than plotted as zero. Overexposure is the slow one; blur is not.</figcaption>
    </figure>
  </section>

  <section>
    <h2>What a session costs</h2>
    <figure>
      <div class="legend">
        <span><i class="swatch" style="background:var(--cat-video)"></i>video uplink</span>
        <span><i class="swatch" style="background:var(--cat-audio)"></i>audio uplink</span>
        <span><i class="swatch" style="background:var(--cat-down)"></i>downlink</span>
      </div>
      <div class="scroll">{bandwidth_chart(rows)}</div>
      <figcaption>Microphone audio is {audio_share:.0f}% of the uplink whenever it is on: 16&nbsp;kHz
      16-bit mono PCM is 256&nbsp;kbit/s and nothing compresses it, while video at {fps}&nbsp;FPS is one
      ~{frame / 1000:.0f}&nbsp;KB JPEG per second. Two other things show up here: darker frames compress
      smaller, so the uplink drops as the room dims; and "very dark" has the <em>highest</em> downlink
      of any condition, because refusing costs more speech than confirming does.</figcaption>
    </figure>
  </section>

  <section>
    <h2>Every trial</h2>
    {trial_table(rows)}
  </section>

  <section>
    <h2>How this was measured</h2>
    <ul>
      <li>Fixture hand images stand in for the webcam, at 640&times;480 JPEG quality 60 &mdash;
      the exact format the browser puts on the wire. Finger counts were verified by eye before
      the fixtures were committed.</li>
      <li>A text frame stands in for saying "scan", sent after four seconds of steady view. Asking
      sooner reports the <em>previous</em> hand: the model answers about a second later using video
      it has already ingested.</li>
      <li>Scoring reads the raw tool call rather than the backend's deduplicated match frame, so
      repeated calls stay visible.</li>
      <li>Lighting and blur are deterministic transforms of the verified fixtures, so the ground
      truth survives the degradation.</li>
    </ul>
    <p class="sub">What it cannot see: the fixtures are static and centred, so this is the easy
    case for framing. In the seven conditions without <code>--noise</code>, no audio is sent at
    all &mdash; which is exactly why the audio failure went unnoticed until it was tested for.</p>
  </section>

  <footer class="mono">scripts/scan_accuracy.py --matrix &middot; rendered by
  scripts/telemetry_report.py</footer>
</div>
"""


def main() -> int:
    if len(sys.argv) < 3:
        return print(__doc__.strip()) or 1
    rows = json.loads(pathlib.Path(sys.argv[1]).read_text())
    out = pathlib.Path(sys.argv[2])
    out.write_text(build(rows))
    print(f"wrote {out} ({out.stat().st_size} bytes)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
