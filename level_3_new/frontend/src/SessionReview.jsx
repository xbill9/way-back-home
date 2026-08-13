// Full-run review, in the app. The corner panels show the last forty seconds,
// which cannot answer "what happened during that run" -- and answering it by
// saving a file and running a Python renderer means leaving the demo. This is
// the same session record, drawn in place.
//
// Charts are inline SVG against the app's own palette rather than the report's:
// this lives inside a neon terminal, not on a document page. Scales are per
// chart and printed on the axis, because a shared scale would flatten latency
// against bandwidth.

const W = 900;
const H = 90;

function niceMax(v) {
    if (!v || v <= 0) return 1;
    const mag = 10 ** Math.floor(Math.log10(v));
    return Math.ceil(v / mag) * mag;
}

function Axis({ max, unit }) {
    return (
        <div className="flex justify-between text-[10px] text-neon-cyan/40 tabular-nums">
            <span>
                0 {unit}
            </span>
            <span>
                {max} {unit}
            </span>
        </div>
    );
}

// Stacked bands, used for the uplink split so video and microphone can be
// compared against each other rather than guessed at from a total.
function StackedArea({ samples, keys, colors, span }) {
    const peak = niceMax(Math.max(...samples.map((s) => keys.reduce((a, k) => a + (s[k] || 0), 0)), 1));
    const running = new Map();
    const layers = keys.map((key, ki) => {
        const top = [];
        const bottom = [];
        for (const s of samples) {
            const base = running.get(s) || 0;
            const value = s[key] || 0;
            running.set(s, base + value);
            const x = (s._t / span) * W;
            top.push(`${x.toFixed(1)},${(H - ((base + value) / peak) * H).toFixed(1)}`);
            bottom.push(`${x.toFixed(1)},${(H - (base / peak) * H).toFixed(1)}`);
        }
        return (
            <polygon
                key={key}
                points={[...top, ...bottom.reverse()].join(' ')}
                fill={colors[ki]}
                opacity="0.7"
            />
        );
    });
    return { peak, svg: <svg viewBox={`0 0 ${W} ${H}`} width="100%" height={H} preserveAspectRatio="none">{layers}</svg> };
}

function Line({ samples, field, span, color }) {
    const points = samples.filter((s) => s[field] != null);
    const peak = niceMax(Math.max(...points.map((s) => s[field]), 1));
    if (points.length < 2) return { peak, svg: null };
    const coords = points
        .map((s) => `${((s._t / span) * W).toFixed(1)},${(H - (s[field] / peak) * H).toFixed(1)}`)
        .join(' ');
    return {
        peak,
        svg: (
            <svg viewBox={`0 0 ${W} ${H}`} width="100%" height={H} preserveAspectRatio="none">
                <polyline
                    points={coords}
                    fill="none"
                    stroke={color}
                    strokeWidth="2"
                    vectorEffect="non-scaling-stroke"
                    strokeLinejoin="round"
                />
            </svg>
        ),
    };
}

function Panel({ title, note, chart, max, unit }) {
    return (
        <div className="border border-neon-cyan/20 bg-black/40 p-3">
            <div className="flex items-baseline justify-between mb-1">
                <span className="text-neon-cyan/60 text-[11px] uppercase tracking-[0.2em]">{title}</span>
                {note && <span className="text-neon-cyan/40 text-[11px] tabular-nums">{note}</span>}
            </div>
            {chart || <div className="text-neon-cyan/30 text-[11px] py-6">never measured</div>}
            <Axis max={max} unit={unit} />
        </div>
    );
}

export function SessionReview({ samples, events, open, onClose }) {
    if (!open) return null;

    const started = samples.length ? samples[0].t : 0;
    const marked = samples.map((s) => ({ ...s, _t: s.t - started }));
    const span = Math.max(marked.length ? marked[marked.length - 1]._t : 1, 1);
    const clock = (ms) => `${Math.floor(ms / 60000)}:${String(Math.floor((ms % 60000) / 1000)).padStart(2, '0')}`;

    const uplink = marked.length > 1 ? StackedArea({ samples: marked, keys: ['video', 'audio'], colors: ['#0ff', '#fcee0a'], span }) : { peak: 1, svg: null };
    const downlink = marked.length > 1 ? Line({ samples: marked, field: 'down', span, color: '#00ff41' }) : { peak: 1, svg: null };
    const detect = Line({ samples: marked, field: 'detectMs', span, color: '#0ff' });
    const fps = Line({ samples: marked, field: 'fps', span, color: '#00ff41' });
    const tokens = Line({ samples: marked, field: 'contextTokens', span, color: '#fcee0a' });
    const output = Line({ samples: marked, field: 'outputTokens', span, color: '#0ff' });

    // Token totals. Context is cumulative already -- the API re-counts the whole
    // context every turn -- so its last value is the total, not a sum. Output
    // accumulates the same way in the hook.
    const last = marked.length ? marked[marked.length - 1] : {};
    const contextTotal = last.contextTokens || 0;
    const outputTotal = last.outputTokens || 0;
    const modality = Object.entries(last.tokensByModality || {}).sort((a, b) => b[1] - a[1]);
    const fmtTok = (n) => (n >= 1000 ? `${(n / 1000).toFixed(1)}k` : String(n || 0));

    const meanUp = marked.length ? marked.reduce((a, s) => a + (s.up || 0), 0) / marked.length : 0;
    const meanAudio = marked.length ? marked.reduce((a, s) => a + (s.audio || 0), 0) / marked.length : 0;

    return (
        <div className="absolute inset-0 z-[70] bg-black/95 backdrop-blur-sm font-mono overflow-y-auto pointer-events-auto">
            <div className="max-w-5xl mx-auto p-6 space-y-4">
                <div className="flex items-baseline justify-between border-b border-neon-cyan/25 pb-3">
                    <div>
                        <h2 className="text-neon-cyan text-xl tracking-[0.2em] uppercase">Session review</h2>
                        <p className="text-neon-cyan/50 text-[11px] mt-1 tabular-nums">
                            {clock(span)} &middot; {samples.length} samples &middot; {events.length} events &middot;
                            mean uplink {meanUp.toFixed(0)} kb/s, {meanUp ? ((meanAudio / meanUp) * 100).toFixed(0) : 0}% microphone
                            &middot; {fmtTok(contextTotal)} context / {fmtTok(outputTotal)} output tokens
                        </p>
                    </div>
                    <button
                        type="button"
                        onClick={onClose}
                        className="px-4 py-1.5 border border-neon-cyan/40 text-neon-cyan text-[11px] uppercase tracking-widest hover:bg-neon-cyan hover:text-black focus:outline-none focus:ring-1 focus:ring-neon-cyan"
                    >
                        close
                    </button>
                </div>

                {samples.length < 2 ? (
                    <p className="text-neon-cyan/50 text-sm py-10">
                        Nothing recorded yet. Run a round, then reopen this.
                    </p>
                ) : (
                    <div className="space-y-3">
                        <Panel
                            title="Uplink"
                            note="cyan video · yellow microphone"
                            chart={uplink.svg}
                            max={uplink.peak}
                            unit="kb/s"
                        />
                        <Panel title="Downlink" chart={downlink.svg} max={downlink.peak} unit="kb/s" />
                        <Panel
                            title="Detect latency"
                            note="frame sent → match"
                            chart={detect.svg}
                            max={detect.peak}
                            unit="ms"
                        />
                        <Panel title="Capture rate" chart={fps.svg} max={fps.peak} unit="fps" />
                        <Panel
                            title="Context tokens"
                            note={modality.length
                                ? modality.map(([m, n]) => `${m.toLowerCase()} ${fmtTok(n)}`).join(' · ')
                                : `${fmtTok(contextTotal)} total`}
                            chart={tokens.svg}
                            max={tokens.peak}
                            unit="tok"
                        />
                        <Panel
                            title="Output tokens"
                            note={`${fmtTok(outputTotal)} generated`}
                            chart={output.svg}
                            max={output.peak}
                            unit="tok"
                        />
                    </div>
                )}

                <div className="border border-neon-cyan/20 bg-black/40 p-3">
                    <div className="text-neon-cyan/60 text-[11px] uppercase tracking-[0.2em] mb-2">Trace</div>
                    <div className="max-h-72 overflow-y-auto space-y-0.5">
                        {events.length === 0 && <div className="text-neon-cyan/30 text-[11px]">no events</div>}
                        {events.map((e) => (
                            <div key={`${e.t}-${e.kind}-${e.text}`} className="text-[11px] flex gap-3">
                                <span className="text-neon-cyan/35 tabular-nums shrink-0 w-12">
                                    {clock(e.t - started)}
                                </span>
                                <span className="text-neon-cyan/70 uppercase shrink-0 w-16">{e.kind}</span>
                                <span className="text-neon-cyan/80 break-words min-w-0">{e.text}</span>
                            </div>
                        ))}
                    </div>
                </div>
            </div>
        </div>
    );
}
