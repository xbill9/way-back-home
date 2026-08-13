// Live transport readout: what the session is costing in bandwidth and how long
// it takes to answer. Both numbers used to be invisible from the UI -- the only
// way to see either was to read the backend log or run scripts/scan_accuracy.py
// after the fact, which is no help while a demo is in front of people.
//
// Form is deliberately stat tiles plus one sparkline each, not gauges: bandwidth
// has no natural maximum, so a filled meter would have to invent a ceiling and
// would read as "80% of something". The sparkline shows the shape (steady vs.
// spiky) without claiming a limit that does not exist.

const HISTORY = 40; // ~40s at the hook's 1Hz sampling

function Sparkline({ series, color, label }) {
    const width = 96;
    const height = 20;
    if (series.length < 2) {
        return <svg width={width} height={height} role="img" aria-label={`${label}: no data yet`} />;
    }
    // Scaled to this series' own peak. Absolute value is on the tile beside it;
    // this is here for shape only.
    const peak = Math.max(...series, 1);
    const step = width / (HISTORY - 1);
    const points = series
        .map((v, i) => {
            const x = (i + (HISTORY - series.length)) * step;
            const y = height - (v / peak) * (height - 2) - 1;
            return `${x.toFixed(1)},${y.toFixed(1)}`;
        })
        .join(' ');
    return (
        <svg width={width} height={height} role="img" aria-label={`${label} over the last ${series.length} seconds`}>
            <polyline
                points={points}
                fill="none"
                stroke={color}
                strokeWidth="2"
                strokeLinejoin="round"
                strokeLinecap="round"
                opacity="0.85"
            />
        </svg>
    );
}

function Row({ label, value, unit, detail, color, sparkline }) {
    return (
        <div className="min-w-0">
            <div className="flex items-baseline justify-between gap-2 min-w-0">
                <span className="text-neon-cyan/50 text-[10px] uppercase tracking-widest shrink-0">{label}</span>
                {sparkline && <span className="shrink-0">{sparkline}</span>}
                <span className="text-right min-w-0 truncate">
                    {/* tabular-nums so the digits stop dancing once a second */}
                    <span className="tabular-nums text-base" style={{ color }}>
                        {value}
                    </span>
                    <span className="text-neon-cyan/40 text-[10px] ml-1">{unit}</span>
                </span>
            </div>
            {/* Its own line, not a sibling of the value: as a sibling a long
                modality breakdown widened the row past the panel and the
                Context figure sat outside the box. */}
            {detail && (
                <div className="text-neon-cyan/55 text-[11px] tabular-nums leading-tight text-right break-words">
                    {detail}
                </div>
            )}
        </div>
    );
}

const fmt = (n) => (n >= 1000 ? `${(n / 1000).toFixed(1)}k` : String(n ?? 0));

// Which modality the context is actually made of. At 1 FPS the image share is
// the number that grows fastest, which is the whole argument about frame rate
// in one line.
function modalitySummary(byModality) {
    const entries = Object.entries(byModality || {});
    if (!entries.length) return null;
    return entries
        .sort((a, b) => b[1] - a[1])
        .map(([mode, n]) => `${mode.toLowerCase()} ${fmt(n)}`)
        .join(' · ');
}

// Pure presentation: the rolling series are accumulated by the hook's sampler,
// which is a timer callback and the right place for it.
// Measured capture rate against what the server asked for. Amber once it drops
// below three quarters of target, which is where a backgrounded tab or a
// struggling encoder shows up first.
function fpsColor(fps, target) {
    if (!target || fps <= 0) return '#0ff8';
    if (fps < target * 0.5) return '#ff003c';
    if (fps < target * 0.75) return '#fcee0a';
    return '#00ff41';
}
function fpsDetail(fps, target) {
    if (!target) return null;
    return fps > 0 && fps < target * 0.75 ? `target ${target}` : null;
}

// Network round trip to our own backend, not to Google. Under ~50ms on a LAN;
// past ~200ms the transport is a real part of what the demo feels like.
function netColor(msValue) {
    if (msValue == null) return '#0ff8';
    if (msValue > 200) return '#ff003c';
    if (msValue > 80) return '#fcee0a';
    return '#00ff41';
}
// Detect with the transport removed. Only meaningful once both halves exist.
function modelMs(netMs, detectMs) {
    if (netMs == null || detectMs == null) return null;
    const model = detectMs - netMs;
    return model > 0 ? model : null;
}

// Three states, not two. "gated" previously showed whenever no gate message had
// arrived -- which is always, when gating is disabled -- so an ungated mic
// sending 256 kbit/s read as one that was sending nothing.
function micState(metrics) {
    // Wake mode never sends audio at all, so open/gated does not apply: the
    // question is whether the microphone is reaching the model, and it is not.
    if (metrics.micMode === 'wake') return { label: '◆ local wake word', color: '#00ff41' };
    if (metrics.micMode === 'off') return { label: '○ off', color: '#0ff6' };
    if (!metrics.micGated) return { label: '● open (ungated)', color: '#00ff41' };
    return metrics.micOpen
        ? { label: '● transmitting', color: '#00ff41' }
        : { label: '○ gated', color: '#0ff6' };
}

export function Telemetry({ metrics, visible, targetFps, live = true }) {
    if (!visible) return null;

    // Latency wears status colour, which is reserved for state and never used
    // for identity elsewhere in this panel. The number itself is the label, so
    // the state is never carried by colour alone.
    const latencyColor = (ms) => {
        if (ms == null) return '#0ff8';
        if (ms > 2000) return '#ff003c';
        if (ms > 1000) return '#fcee0a';
        return '#00ff41';
    };
    const ms = (v) => (v == null ? '--' : v);

    return (
        <div className="w-full border border-neon-cyan/25 bg-black/55 backdrop-blur-sm px-3 py-2 font-mono space-y-1.5">
            {/* The header says whether these numbers mean anything yet, so a
                panel full of zeroes reads as "not started" rather than "broken". */}
            <div className="flex items-baseline justify-between border-b border-neon-cyan/15 pb-1">
                <span className="text-neon-cyan/40 text-[10px] uppercase tracking-[0.3em]">Transport</span>
                <span className="text-[10px] uppercase tracking-widest" style={{ color: live ? '#00ff41' : '#0ff6' }}>
                    {live ? 'live' : 'idle'}
                </span>
            </div>

            <Row
                label="Uplink"
                value={metrics.upKbps.toFixed(1)}
                unit="kb/s"
                detail={`vid ${metrics.videoKbps.toFixed(0)} · aud ${metrics.audioKbps.toFixed(0)}`}
                color="#0ff"
                sparkline={<Sparkline series={metrics.upHistory} color="#0ff" label="Uplink" />}
            />
            <Row
                label="Downlink"
                value={metrics.downKbps.toFixed(1)}
                unit="kb/s"
                color="#00ff41"
                sparkline={<Sparkline series={metrics.downHistory} color="#00ff41" label="Downlink" />}
            />

            <div className="border-t border-neon-cyan/15 pt-1.5 space-y-1.5">
                {/* Frames actually put on the wire, not the rate we asked for.
                    They diverge when the tab is backgrounded or the encoder
                    cannot keep up, and that divergence is invisible otherwise --
                    detection just quietly gets worse. */}
                <Row
                    label="Capture"
                    value={metrics.fps.toFixed(1)}
                    unit="fps"
                    detail={fpsDetail(metrics.fps, targetFps)}
                    color={fpsColor(metrics.fps, targetFps)}
                />
                {/* Network only: a ping echoed by the backend without touching
                    the model. Subtract it from Detect and what is left is the
                    model thinking -- which is the number worth arguing about. */}
                <Row
                    label="Net"
                    value={ms(metrics.netMs)}
                    unit="ms"
                    color={netColor(metrics.netMs)}
                />
                <Row label="Detect" value={ms(metrics.detectMs)} unit="ms" color={latencyColor(metrics.detectMs)} />
                {/* Detect minus Net: how long the model took with the transport
                    removed, and the number most worth reading here. Called
                    "think" rather than "model" because the header now names the
                    model itself, and two things labelled MODEL on one screen is
                    one too many. */}
                <Row
                    label="Think"
                    value={ms(modelMs(metrics.netMs, metrics.detectMs))}
                    unit="ms"
                    color={latencyColor(modelMs(metrics.netMs, metrics.detectMs))}
                />
                <Row label="Speak" value={ms(metrics.speakMs)} unit="ms" color={latencyColor(metrics.speakMs)} />
            </div>

            {/* State reads as form as well as colour: the word changes too, so
                it survives a colourblind viewer and a monochrome projector. */}
            <div className="border-t border-neon-cyan/15 pt-1.5 flex items-center justify-between">
                <span className="text-neon-cyan/50 text-[10px] uppercase tracking-widest">Mic</span>
                <span
                    className="text-[11px] uppercase tracking-widest"
                    style={{ color: live ? micState(metrics).color : '#0ff6' }}
                >
                    {live ? micState(metrics).label : '\u25cb idle'}
                </span>
            </div>

            {/* The two numbers the gate is actually comparing, live. Present
                only with ?gate=1. Reading "level 0.004 vs opens 0.061" off the
                screen while speaking is what tuning needs -- every previous
                attempt was tuned against synthetic signals and was wrong. */}
            {metrics.gateLevel != null && (
                <div className="flex items-center justify-between text-[11px] tabular-nums">
                    <span className="text-neon-cyan/55">level {metrics.gateLevel.toFixed(4)}</span>
                    <span
                        style={{
                            color:
                                metrics.gateLevel > metrics.gateOpensAt ? '#00ff41' : '#0ff6',
                        }}
                    >
                        opens at {metrics.gateOpensAt?.toFixed(4)}
                    </span>
                </div>
            )}

            {/* Tokens. Context is the latest cumulative prompt count, not a sum:
                the API re-counts the whole context every turn, so adding them up
                would multiply it by the number of turns. Output is a real sum. */}
            <div className="border-t border-neon-cyan/15 pt-1.5 space-y-1.5">
                <Row
                    label="Context"
                    value={fmt(metrics.contextTokens)}
                    unit="tok"
                    detail={modalitySummary(metrics.tokensByModality)}
                    color="#0ff"
                />
                <Row label="Output" value={fmt(metrics.outputTokens)} unit="tok" color="#0ff" />
            </div>

            <div className="text-neon-cyan/40 text-[10px] leading-tight pt-0.5">
                detect: frame sent → match · speak: match → first audio
            </div>
        </div>
    );
}
