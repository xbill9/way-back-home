import { useEffect, useRef, useState } from 'react';

// What the session is actually doing, in order: what the model heard, what it
// said, when it called a tool, when the mic gate opened, when playback was cut
// off by barge-in. All of it already passes through the socket hook -- until now
// it only reached the browser console, which is no use to anyone watching a demo
// on a projector.
//
// Collapsed by default. Expanded it is the fastest way to answer "why did it do
// that", which during this project has usually been a question about ordering:
// a tool call arriving before its confirmation, or a transcript arriving a whole
// turn late.

const KIND_COLOR = {
    you: '#0ff',
    scanner: '#00ff41',
    tool: '#fcee0a',
    match: '#00ff41',
    mic: '#0ff',
    'barge-in': '#fcee0a',
    error: '#ff003c',
    metal: '#fcee0a',
};

export function EventTrace({ events, visible, onSave, onReview, onClear }) {
    const [open, setOpen] = useState(false);
    const scroller = useRef(null);

    useEffect(() => {
        // Pin to the newest line while open, the way a log viewer should.
        if (open && scroller.current) {
            scroller.current.scrollTop = scroller.current.scrollHeight;
        }
    }, [events, open]);

    if (!visible) return null;

    const recent = open ? events.slice(-60) : events.slice(-3);

    return (
        <div className="w-full min-h-0 flex flex-col border border-neon-cyan/25 bg-black/55 backdrop-blur-sm font-mono pointer-events-auto">
            <div className="flex items-center justify-between px-3 py-1.5">
                <button
                    type="button"
                    onClick={() => setOpen((v) => !v)}
                    className="flex-1 flex items-center justify-between text-neon-cyan/50 text-[10px] uppercase tracking-[0.3em] hover:text-neon-cyan focus:outline-none focus:ring-1 focus:ring-neon-cyan/60"
                    aria-expanded={open}
                >
                    <span>Trace</span>
                    <span className="tracking-normal">{open ? '▾' : `▸ ${events.length}`}</span>
                </button>
                {/* The panels only ever hold the last 40s; this writes the whole
                    run to disk, for scripts/telemetry_view.py to render. */}
                {onReview && (
                    <button
                        type="button"
                        onClick={onReview}
                        title="Review the whole run"
                        className="ml-3 text-neon-cyan/40 text-[10px] uppercase tracking-widest hover:text-neon-cyan focus:outline-none focus:ring-1 focus:ring-neon-cyan/60"
                    >
                        review
                    </button>
                )}
                {onClear && (
                    <button
                        type="button"
                        onClick={onClear}
                        title="Discard the recorded run so the next one starts clean"
                        className="ml-3 text-neon-cyan/40 text-[10px] uppercase tracking-widest hover:text-neon-cyan focus:outline-none focus:ring-1 focus:ring-neon-cyan/60"
                    >
                        clear
                    </button>
                )}
                {onSave && (
                    <button
                        type="button"
                        onClick={onSave}
                        title="Download this session as JSON"
                        className="ml-3 text-neon-cyan/40 text-[10px] uppercase tracking-widest hover:text-neon-cyan focus:outline-none focus:ring-1 focus:ring-neon-cyan/60"
                    >
                        save
                    </button>
                )}
            </div>

            <div
                ref={scroller}
                className={`px-3 pb-2 space-y-0.5 overflow-y-auto min-h-0 ${open ? 'flex-1' : 'max-h-16'}`}
            >
                {recent.length === 0 && (
                    <div className="text-neon-cyan/30 text-[10px]">no events yet</div>
                )}
                {recent.map((e) => (
                    <div key={`${e.t}-${e.kind}-${e.text}`} className="text-[10px] leading-snug flex gap-2">
                        <span className="text-neon-cyan/30 tabular-nums shrink-0">
                            {new Date(e.t).toLocaleTimeString('en-GB', { hour12: false }).slice(3)}
                        </span>
                        <span
                            className="uppercase shrink-0 w-14 tracking-wider"
                            style={{ color: KIND_COLOR[e.kind] || '#0ff8' }}
                        >
                            {e.kind}
                        </span>
                        <span className="text-neon-cyan/70 break-words min-w-0">{e.text}</span>
                    </div>
                ))}
            </div>
        </div>
    );
}
