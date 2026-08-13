import React, { useState, useEffect, useRef } from 'react';
import { useGeminiSocket } from './useGeminiSocket';
import { playHeavyMetalSting } from './heavyMetalSting';
import { Telemetry } from './Telemetry';
import { EventTrace } from './EventTrace';
import { SessionReview } from './SessionReview';

const SEQUENCE_LENGTH = 4;
const ROUND_TIME = 65;

// Representative values for ?hud=1, so the panels can be laid out and checked
// without a webcam or a billed session. Deliberately the awkward cases: a long
// modality breakdown, four-digit token counts, a three-digit latency.
const SAMPLE_METRICS = {
    upKbps: 333.4, videoKbps: 128.6, audioKbps: 204.8, downKbps: 83.2,
    fps: 1.0, netMs: 12, detectMs: 840, speakMs: 310,
    micOpen: true, micGated: true,
    upHistory: [120, 180, 333, 300, 210, 333, 290, 333, 310, 333],
    downHistory: [10, 60, 83, 40, 90, 83, 20, 75, 83, 60],
    contextTokens: 3786, outputTokens: 1204,
    tokensByModality: { TEXT: 1728, IMAGE: 1386, AUDIO: 138 },
    gateLevel: 0.0042, gateOpensAt: 0.0126,
};
const SAMPLE_EPOCH = 1786650000000;
const SAMPLE_SAMPLES = Array.from({ length: 90 }, (_, i) => {
    const talking = (i > 25 && i < 40) || (i > 60 && i < 72);
    return {
        t: SAMPLE_EPOCH + i * 1000,
        up: 128 + (talking ? 205 : 0), down: 20 + (talking ? 70 : 0),
        video: 128, audio: talking ? 205 : 0,
        fps: i < 75 ? 1 : 0.4,
        netMs: 12, detectMs: talking ? 700 + (i % 7) * 90 : null,
        speakMs: talking ? 320 : null,
        contextTokens: 400 + i * 40, outputTokens: i * 9,
    };
});
const SAMPLE_EVENTS = [
    { t: SAMPLE_EPOCH + 82000, kind: 'scanner', text: 'Scanner Online.' },
    { t: SAMPLE_EPOCH + 85000, kind: 'you', text: 'scan' },
    { t: SAMPLE_EPOCH + 85800, kind: 'tool', text: 'report_digit({"count":3})' },
    { t: SAMPLE_EPOCH + 85900, kind: 'match', text: 'digit 3' },
    { t: SAMPLE_EPOCH + 87000, kind: 'scanner', text: 'Three digits.' },
    { t: SAMPLE_EPOCH + 89000, kind: 'mic', text: 'gated' },
];

const generateSequence = () => {
    const nums = new Set();
    while (nums.size < SEQUENCE_LENGTH) {
        nums.add(Math.floor(Math.random() * 5) + 1); // 1-5
    }
    return Array.from(nums);
};

export default function BiometricLock() {
    const [sequence, setSequence] = useState([]);
    const [inputProgress, setInputProgress] = useState([]);
    const [status, setStatus] = useState('IDLE'); // IDLE, SCANNING, SUCCESS, FAIL
    const [timeLeft, setTimeLeft] = useState(ROUND_TIME);

    // Effect to update participant status on success
    useEffect(() => {
        if (status === 'SUCCESS') {
            const updateStatus = async () => {
                try {
                    // Use fetch instead of import to avoid build/runtime errors if file is missing
                    console.log('[BiometricLock] Attempting to fetch config.json...');
                    let config = null;
                    try {
                        const configResponse = await fetch('/config.json');
                        if (configResponse.ok) {
                            config = await configResponse.json();
                        } else {
                            console.log('[BiometricLock] config.json not found (status:', configResponse.status, ')');
                        }
                    } catch (e) {
                        console.log('[BiometricLock] Error fetching config.json:', e);
                    }

                    if (config && config.participant_id && config.api_base) {
                        console.log('[BiometricLock] found config.json:', config);

                        const response = await fetch(`${config.api_base}/participants/${config.participant_id}`);
                        if (!response.ok) {
                            console.error('[BiometricLock] GET participant failed:', response.status);
                            return;
                        }

                        const data = await response.json();
                        console.log('[BiometricLock] GET participant success:', data);

                        // This is level 3, so level_3_complete is the flag that
                        // changes -- and it has to be the same flag the PATCH
                        // below sends, or completion_percentage reports a level
                        // the request never actually sets.
                        const updatedData = { ...data, level_3_complete: true };

                        // Calculate completion percentage
                        let labsCompleted = 0;
                        if (updatedData.level_1_complete) labsCompleted++;
                        if (updatedData.level_2_complete) labsCompleted++;
                        if (updatedData.level_3_complete) labsCompleted++;
                        if (updatedData.level_4_complete) labsCompleted++;
                        if (updatedData.level_5_complete) labsCompleted++;

                        const completion_percentage = labsCompleted * 20;
                        const patchPayload = {
                            level_3_complete: true,
                            completion_percentage: completion_percentage
                        };

                        console.log('[BiometricLock] PATCH payload:', patchPayload);

                        const patchResponse = await fetch(`${config.api_base}/participants/${config.participant_id}`, {
                            method: 'PATCH',
                            headers: {
                                'Content-Type': 'application/json',
                            },
                            body: JSON.stringify(patchPayload),
                        });

                        if (patchResponse.ok) {
                            console.log('[BiometricLock] PATCH success');
                        } else {
                            console.error('[BiometricLock] PATCH failed:', patchResponse.status);
                        }
                    } else {
                        console.log('[BiometricLock] config.json missing required fields or not found');
                    }
                } catch (err) {
                    // Config not found or API error, ignore as per instructions
                    console.log('Optional config not found or update failed:', err);
                }
            };
            updateStatus();
        }
    }, [status]);

    const videoRef = useRef(null);
    // ADK backend expects /ws/{user_id}/{session_id}
    //
    // Fresh session per ROUND, not per mount. The component stays mounted across
    // rounds, so a per-mount id meant every round after the first reconnected
    // with the same session_id -- and the Runner's auto_create_session=True
    // *resumes* an existing session rather than creating one. The model then
    // carried the previous round's conversation into the new one: round 2 opened
    // mid-scan with "Five digits." instead of "Scanner Online.", and by round 3
    // it answered once and went silent for the rest of the session.
    //
    // crypto.randomUUID() rather than Math.random().toString(36).substring(7),
    // which yielded about five base-36 characters -- collidable across
    // concurrent users, who all share the hardcoded "user1".
    const [sessionId, setSessionId] = useState(() => crypto.randomUUID());

    // Dynamic WebSocket URL handling for Cloud Shell / Localhost
    // If protocol is https (Cloud Shell), use wss. If http (localhost), use ws.
    // window.location.host includes the port if present.
    // Layout of the telemetry panels can be checked without a session, and
    // without a webcam or a billed connection, via ?hud=1.
    const forceHud = new URLSearchParams(window.location.search).has('hud');
    const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
    const buildWsUrl = (id) => `${protocol}//${window.location.host}/ws/user1/${id}`;
    const wsUrl = buildWsUrl(sessionId);

    const { status: socketStatus, isMock, config, metrics, events, sessionSamples, saveSession, connect, disconnect, startStream, stopStream } = useGeminiSocket(wsUrl, {
        onDigitDetected: (detected) => {
            if (status !== 'SCANNING') return;

            setInputProgress((prev) => {
                const targetIndex = prev.length;
                const targetValue = sequence[targetIndex];

                if (detected === targetValue) {
                    const newProgress = [...prev, detected];
                    if (newProgress.length === SEQUENCE_LENGTH) {
                        setStatus('SUCCESS');
                    }
                    return newProgress;
                }
                return prev;
            });
        },
        onSystemError: (message) => {
            console.error('SYSTEM ERROR:', message);
            setStatus('SYSTEM_ERROR');
        },
        onHeavyMetal: (message) => {
            console.log('🤘 HEAVY METAL MODE ACTIVATED:', message);
            setStatus('HEAVY_METAL');
        }
    });

    const [reviewOpen, setReviewOpen] = useState(false);
    // Stays up after a round ends: reviewing a run you have just finished is the
    // main reason to open it, and the panels vanish the moment the socket closes.
    const hudVisible = socketStatus === 'CONNECTED' || forceHud || sessionSamples.length > 0;
    const hudMetrics = socketStatus === 'CONNECTED' ? metrics : SAMPLE_METRICS;
    const hudEvents = socketStatus === 'CONNECTED' || sessionSamples.length ? events : SAMPLE_EVENTS;
    const reviewSamples = sessionSamples.length ? sessionSamples : (forceHud ? SAMPLE_SAMPLES : []);

    // Handle Game Start
    const startRound = () => {
        const newSeq = generateSequence();
        setSequence(newSeq);
        setInputProgress([]);
        setTimeLeft(ROUND_TIME);
        setStatus('SCANNING');

        // A new round is a new conversation. The id is passed straight to
        // connect() rather than read back from state, which would still hold the
        // previous round's value in this tick.
        const freshId = crypto.randomUUID();
        setSessionId(freshId);
        connect(buildWsUrl(freshId));
    };

    useEffect(() => {
        if (status === 'SCANNING') {
            startStream(videoRef.current);
        } else if (status === 'SUCCESS' || status === 'FAIL' || status === 'SYSTEM_ERROR' || status === 'HEAVY_METAL') {
            stopStream();
            disconnect();

            if (status === 'HEAVY_METAL') {
                // Synthesised locally. This used to hotlink an archive.org mp3
                // that 404s, so the easter egg had no audio payoff at all.
                playHeavyMetalSting().catch(e => console.error('Sting failed:', e));
            }
        }
    }, [status, startStream, stopStream, disconnect]);

    // Timer
    useEffect(() => {
        let interval;
        if (status === 'SCANNING') {
            interval = setInterval(() => {
                setTimeLeft((t) => {
                    if (t <= 1) {
                        setStatus('FAIL');
                        return 0;
                    }
                    return t - 1;
                });
            }, 1000);
        }
        return () => clearInterval(interval);
    }, [status]);

    // Game Logic - Input Handling
    const [permissionDenied, setPermissionDenied] = useState(false);
    const [initiationWarning, setInitiationWarning] = useState(false);

    // Initialize random positions for particles using useState lazy initializer
    // This is allowed by purity rules as it only runs once on mount
    const [particles] = useState(() => Array.from({ length: 20 }, (_, i) => ({
        id: i,
        left: `${Math.random() * 100}%`,
        top: `${Math.random() * 100}%`,
        duration: `${Math.random() * 2 + 1}s`
    })));

    const handleInitiateOverride = async () => {
        try {
            // Request Camera and Microphone Permission immediately
            const stream = await navigator.mediaDevices.getUserMedia({ video: true, audio: true });

            // If we get here, permission granted. 
            // Stop this temporary stream so startStream() can get a fresh one later (or we could pass it, but simpler to just release)
            stream.getTracks().forEach(track => track.stop());

            setPermissionDenied(false);

            // Show Warning/Startup sequence
            setInitiationWarning(true);
            startRound(); // Start camera/game immediately behind overlay

            setTimeout(() => {
                setInitiationWarning(false);
            }, 5000);
        } catch (err) {
            console.error("Camera permission denied:", err);
            setPermissionDenied(true);
        }
    };

    return (
        <div className="relative w-full h-screen bg-black overflow-hidden font-mono text-neon-cyan select-none">

            {/* Transport readout. Only while a socket is actually open -- zeroes
                on an idle screen look like a broken instrument. */}
            {/* One right-hand column so the two panels share a width and the
                trace can never grow into the stats box: the column owns the
                geometry, the panels only fill it. `?hud=1` shows them without a
                live socket, which is how the layout gets checked without opening
                a billed session. */}
            <div className="absolute top-4 right-4 bottom-4 z-40 w-72 flex flex-col gap-3 pointer-events-none">
                <Telemetry metrics={hudMetrics} visible={hudVisible} targetFps={config.video_fps} />
                <div className="mt-auto min-h-0 flex flex-col">
                    <EventTrace events={hudEvents} visible={hudVisible} onSave={saveSession} onReview={() => setReviewOpen(true)} />
                </div>
            </div>

            <SessionReview
                samples={reviewSamples}
                events={hudEvents}
                open={reviewOpen}
                onClose={() => setReviewOpen(false)}
            />


            {/* Mock Server Banner Ticker */}
            {isMock && (
                <div className="absolute top-1/2 -translate-y-1/2 left-0 right-0 z-[100] overflow-hidden bg-amber-500/90 border-y-2 border-amber-300 backdrop-blur-sm" style={{ height: '2.5rem' }}>
                    <div
                        className="whitespace-nowrap text-black font-bold text-sm uppercase tracking-widest flex items-center h-full"
                        style={{
                            animation: 'mock-ticker 18s linear infinite',
                        }}
                    >
                        {/* Repeat the message so the scroll feels seamless */}
                        {Array(6).fill('⚠ MOCK SERVER ACTIVE — THIS IS A SIMULATION — NO ACTIONS WILL BE EXECUTED    ').join('')}
                    </div>
                </div>
            )}
            {/* ... (background video remains same) ... */}
            <video
                ref={videoRef}
                muted
                playsInline
                className={`absolute top-0 left-0 w-full h-full object-cover z-0 opacity-50 grayscale transition-all duration-1000 ${status === 'SUCCESS' ? 'grayscale-0 opacity-100 blur-sm' :
                    status === 'FAIL' ? 'grayscale opacity-20 blur-md' : ''
                    }`}
            />

            {/* Scanlines Overlay - Disable on end game for clear view */}
            {status === 'SCANNING' && <div className="scanlines z-10"></div>}

            {/* Permission Denied Overlay */}
            {permissionDenied && (
                <div className="absolute inset-0 z-50 flex items-center justify-center bg-black/90 backdrop-blur-md animate-fade-in">
                    <div className="text-center border border-red-500 p-10 rounded bg-red-950/20 box-shadow-xl">
                        <h1 className="text-4xl font-bold text-red-500 mb-4 animate-pulse">ACCESS DENIED</h1>
                        <p className="text-xl text-red-300 mb-8">BIOMETRIC SENSOR OFFLINE</p>
                        <p className="text-sm text-gray-400 mb-8">Camera access required for neural handshake.</p>
                        <button
                            onClick={() => setPermissionDenied(false)}
                            className="px-6 py-2 border border-red-500 text-red-500 hover:bg-red-500 hover:text-white transition-colors"
                        >
                            ACKNOWLEDGE
                        </button>
                    </div>
                </div>
            )}

            {/* Initialization Warning Overlay */}
            {initiationWarning && (
                <div className="absolute inset-0 z-50 flex items-center justify-center bg-black/60 backdrop-blur-sm animate-fade-in">
                    <div className="text-center max-w-2xl px-8">
                        <h1 className="text-4xl font-bold text-yellow-500 mb-6 animate-pulse">INITIALIZING NEURAL LINK...</h1>
                        <div className="text-xl text-yellow-200/80 mb-8 space-y-4 font-mono">
                            <p>ESTABLISHING SECURE CHANNEL.</p>
                            <p className="border-t border-b border-yellow-500/30 py-4">
                                WAIT FOR AUDIO CONFIRMATION:<br />
                                <span className="text-white font-bold">"Biometric Scanner Online"</span>
                            </p>
                        </div>
                        <div className="w-full h-1 bg-yellow-900 rounded-full overflow-hidden">
                            <div className="h-full bg-yellow-400 animate-[width_3s_linear_forwards]" style={{ width: '0%' }}></div>
                        </div>
                    </div>
                </div>
            )}

            {/* Success/Fail Overlays with Dynamic Effects */}
            {status === 'SUCCESS' && (
                <div className="absolute inset-0 z-30 flex items-center justify-center bg-green-900/40 backdrop-blur-sm animate-fade-in">
                    <div className="text-center">
                        <h1 className="text-8xl font-black text-white drop-shadow-[0_0_30px_rgba(0,255,0,0.8)] animate-bounce">
                            NEURAL SYNC COMPLETE
                        </h1>
                        <p className="text-2xl text-neon-green mt-4 tracking-[1em] animate-pulse">
                            DRIFT STABLE // FLEET ACTIVE
                        </p>
                        <button
                            onClick={handleInitiateOverride}
                            className="mt-12 px-8 py-3 bg-black/80 border border-neon-green text-neon-green hover:bg-neon-green hover:text-black transition-all"
                        >
                            RE-SYNC
                        </button>
                    </div>
                    {/* Confetti-like particles (simple CSS circles) */}
                    <div className="absolute inset-0 pointer-events-none overflow-hidden">
                        {particles.map((p) => (
                            <div key={p.id} className="absolute w-2 h-2 bg-neon-green rounded-full animate-ping"
                                style={{
                                    left: p.left,
                                    top: p.top,
                                    animationDuration: p.duration
                                }}
                            />
                        ))}
                    </div>
                </div>
            )}

            {status === 'FAIL' && (
                <div className="absolute inset-0 z-30 flex items-center justify-center bg-red-900/60 backdrop-blur-sm animate-shake">
                    <div className="text-center">
                        <h1 className="text-9xl font-black text-red-600 drop-shadow-[0_0_50px_rgba(255,0,0,1)] glitch-text">
                            CRITICAL FAIL
                        </h1>
                        <p className="text-3xl text-red-400 mt-4 tracking-widest uppercase font-bold">
                            Time Expired / Protocol Breach
                        </p>
                        <button
                            onClick={handleInitiateOverride}
                            className="mt-12 px-8 py-3 bg-black/80 border border-red-500 text-red-500 hover:bg-red-500 hover:text-black transition-all"
                        >
                            RETRY SEQUENCE
                        </button>
                    </div>
                </div>
            )}

            {status === 'SYSTEM_ERROR' && (
                <div className="absolute inset-0 z-[60] flex items-center justify-center bg-red-950 animate-pulse">
                    <div className="text-center p-12 border-4 border-red-600 bg-black shadow-[0_0_100px_rgba(255,0,0,0.5)]">
                        <h1 className="text-7xl font-black text-red-600 mb-6 tracking-tighter">
                            SYSTEM ERROR
                        </h1>
                        <div className="h-1 w-full bg-red-600 mb-8"></div>
                        <p className="text-2xl text-red-400 font-bold mb-4 uppercase">
                            Neural Link Corruption Detected
                        </p>
                        <p className="text-lg text-red-500/80 mb-12 font-mono">
                            REASON: CRITICAL PROTOCOL VIOLATION (OFFENSIVE GESTURE)
                        </p>
                        <div className="text-sm text-red-700 animate-pulse">
                            TERMINATING SESSION...
                        </div>
                        <button
                            onClick={() => window.location.reload()}
                            className="mt-12 px-10 py-4 border-2 border-red-600 text-red-600 hover:bg-red-600 hover:text-black font-bold transition-all"
                        >
                            REBOOT SYSTEM
                        </button>
                    </div>
                </div>
            )}

            {status === 'HEAVY_METAL' && (
                <div className="absolute inset-0 z-[70] flex items-center justify-center bg-black animate-fade-in">
                    {/* CSS-only strobe/scanline field. Replaces a giphy hotlink
                        that 404s -- so this rendered nothing at all -- and it
                        works offline and under a strict CSP. */}
                    <div className="absolute inset-0 opacity-30 animate-pulse-fast bg-[repeating-linear-gradient(0deg,#fff_0px,#fff_1px,transparent_1px,transparent_4px),repeating-linear-gradient(90deg,#ff003c_0px,#ff003c_2px,transparent_2px,transparent_9px)] contrast-150"></div>
                    <div className="relative text-center p-12 border-8 border-yellow-500 bg-black/80 shadow-[0_0_150px_rgba(255,255,0,0.4)] animate-shake">
                        <h1 className="text-8xl font-black text-yellow-500 mb-4 italic tracking-tighter drop-shadow-[0_0_20px_rgba(255,255,0,0.8)]">
                            HEAVY METAL OVERRIDE
                        </h1>
                        <div className="h-2 w-full bg-gradient-to-r from-transparent via-yellow-500 to-transparent mb-8"></div>
                        <p className="text-4xl text-white font-black mb-8 uppercase tracking-[0.3em] animate-pulse">
                            🤘 PROTOCOL: SABBATH 🤘
                        </p>
                        <div className="flex justify-center gap-12 mb-12">
                           <div className="text-6xl animate-bounce">🎸</div>
                           <div className="text-6xl animate-bounce [animation-delay:0.2s]">⚡</div>
                           <div className="text-6xl animate-bounce [animation-delay:0.4s]">🎸</div>
                        </div>
                        <p className="text-xl text-yellow-400 font-mono mb-12">
                            NEURAL LINK SATURATED WITH PURE DOOM
                        </p>
                        <button
                            onClick={() => window.location.reload()}
                            className="px-12 py-4 bg-yellow-500 text-black font-black text-2xl hover:bg-white hover:scale-110 transition-all shadow-[0_0_30px_rgba(255,255,0,0.6)]"
                        >
                            REBOOT & ROCK AGAIN
                        </button>
                    </div>
                </div>
            )}

            {/* Main HUD */}
            <div className={`relative z-20 flex flex-col items-center justify-between h-full py-10 px-4 transition-opacity duration-500 ${status !== 'SCANNING' && status !== 'IDLE' && status !== 'HEAVY_METAL' ? 'opacity-20 blur-sm' : 'opacity-100'}`}>

                {/* Header */}
                <div className="w-full max-w-4xl flex justify-between items-center border-b-2 border-neon-cyan/50 pb-4 bg-black/60 backdrop-blur-sm p-6 rounded-t-xl">
                    <div>
                        <h2 className="text-4xl font-black text-white tracking-[0.2em] mb-2 drop-shadow-[0_0_10px_rgba(255,255,255,0.5)]">MISSION ALPHA</h2>
                        <h1 className="text-xl font-bold tracking-widest text-glow text-neon-cyan">SECURITY PROTOCOL: LEVEL 5</h1>
                        <div className="text-xs text-neon-cyan/70">Bio-Signature Required</div>
                    </div>
                    <div className={`px-4 py-2 text-xl font-bold border animate-pulse ${status === 'IDLE' ? 'border-red-500 text-red-500' :
                        status === 'SCANNING' && socketStatus === 'CONNECTED' ? 'border-yellow-400 text-yellow-400' :
                            'border-red-600 text-red-600'
                        }`}>
                        {status === 'IDLE' && 'DISSOCIATED'}
                        {status === 'SCANNING' && (
                            <div className="flex items-center gap-3">
                                {socketStatus === 'CONNECTED' ? (
                                    <>
                                        <span>NEURAL SYNC INITIALIZED</span>
                                        {/* Network Pulse Indicator - Subtle Radar Blip */}
                                        <div className="relative flex items-center justify-center w-6 h-6 ml-1">
                                            {/* Expanding Ping Ring */}
                                            <div 
                                                className="absolute w-full h-full rounded-full border border-neon-cyan/40"
                                                style={{
                                                    animation: `ring-pulse ${config.heartbeat_interval}s infinite ease-out`
                                                }}
                                            ></div>
                                            {/* Central Status Dot */}
                                            <div 
                                                className="relative w-1.5 h-1.5 rounded-full bg-neon-cyan shadow-[0_0_8px_#00ffff]"
                                                style={{
                                                    animation: `heartbeat-pulse ${config.heartbeat_interval}s infinite linear`
                                                }}
                                            ></div>
                                        </div>
                                    </>
                                ) : (
                                    'NEURAL LINK DROPPED // OFFLINE'
                                )}
                            </div>
                        )}
                    </div>
                </div>

                {/* Center Challenge */}
                <div className="flex-1 flex flex-col items-center justify-center gap-12 w-full max-w-4xl">

                    {status === 'IDLE' && (
                        <button
                            onClick={handleInitiateOverride}
                            className="px-12 py-6 text-2xl font-bold border-2 border-neon-cyan hover:bg-neon-cyan hover:text-black transition-all shadow-[0_0_20px_rgba(0,255,255,0.3)] animate-pulse"
                        >
                            INITIATE NEURAL SYNC
                        </button>
                    )}

                    {status === 'SCANNING' && (
                        <>
                            {/* The Sequence */}
                            <div className="flex gap-6">
                                {sequence.map((num, idx) => {
                                    const isMatched = idx < inputProgress.length;
                                    return (
                                        <div key={idx} className={`w-24 h-32 flex items-center justify-center text-6xl font-bold border-4 rounded-lg transition-all duration-300 ${isMatched
                                            ? 'border-neon-green text-neon-green bg-neon-green/10 shadow-[0_0_30px_rgba(0,255,65,0.5)] transform scale-110'
                                            : 'border-neon-cyan text-neon-cyan bg-black/50 shadow-[0_0_15px_rgba(0,255,255,0.2)] animate-pulse-fast'
                                            }`}>
                                            {num}
                                        </div>
                                    )
                                })}
                            </div>

                            {/* Feedback Slots */}
                            <div className="flex gap-6 opacity-80">
                                {Array(SEQUENCE_LENGTH).fill(0).map((_, idx) => {
                                    const isFilled = idx < inputProgress.length;
                                    const isCurrent = idx === inputProgress.length;
                                    return (
                                        <div key={idx} className={`w-24 h-4 border-b-4 transition-all ${isFilled ? 'border-neon-green' :
                                            isCurrent ? 'border-white animate-pulse' : 'border-gray-700'
                                            }`}></div>
                                    )
                                })}
                            </div>

                            {/* Instruction Text */}
                            <div className="text-center animate-pulse mt-8">
                                <p className="text-neon-cyan/80 text-lg uppercase tracking-widest border border-neon-cyan/30 px-6 py-2 rounded bg-black/40">
                                    Show Hand & Say <span className="font-bold text-white">"CALIBRATE"</span> / <span className="font-bold text-white">"SCAN"</span>
                                </p>
                            </div>
                        </>
                    )}
                </div>

                {/* Footer / Timer */}
                <div className="w-full max-w-4xl grid grid-cols-3 items-end">
                    <div className="text-xl">
                        SINGLE STAGE OPERATION
                    </div>

                    <div className="flex justify-center">
                        {status === 'SCANNING' && (
                            <div className={`text-6xl font-black tabular-nums tracking-tighter ${timeLeft <= 10 ? 'text-red-500 animate-bounce' : 'text-white'}`}>
                                00:{timeLeft.toString().padStart(2, '0')}
                            </div>
                        )}
                    </div>

                    <div className="text-right text-xl">
                        STATUS: {socketStatus}
                    </div>
                </div>

            </div>
        </div>
    );
}
