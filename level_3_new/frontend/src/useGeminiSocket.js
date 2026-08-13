import { useState, useRef, useCallback, useEffect } from "react";
import { AudioStreamer } from "./audioStreamer";
import { AudioRecorder } from "./audioRecorder";
import { WakeWordListener, isSupported as wakeWordSupported } from "./wakeWord";

export function useGeminiSocket(
  url,
  { onDigitDetected, onSystemError, onHeavyMetal, onDropped } = {},
) {
  const [status, setStatus] = useState("DISCONNECTED");
  const [isMock, setIsMock] = useState(false);

  const onDigitDetectedRef = useRef(onDigitDetected);
  const onSystemErrorRef = useRef(onSystemError);
  const onHeavyMetalRef = useRef(onHeavyMetal);
  const onDroppedRef = useRef(onDropped);
  // Set just before we close on purpose, so an unexpected close is
  // distinguishable from ending a round.
  const closingOnPurpose = useRef(false);
  useEffect(() => {
    onDigitDetectedRef.current = onDigitDetected;
    onSystemErrorRef.current = onSystemError;
    onHeavyMetalRef.current = onHeavyMetal;
    onDroppedRef.current = onDropped;
  }, [onDigitDetected, onSystemError, onHeavyMetal, onDropped]);

  const ws = useRef(null);
  const streamRef = useRef(null);
  const intervalRef = useRef(null);
  // Lazily constructed. `useRef(new X())` evaluates the argument on every
  // render and throws the result away -- harmless for a plain object, but
  // these own audio hardware, and it leaked an AudioContext per render until
  // Chrome refused to make more.
  // Built on first use, never during render. `useRef(new X())` evaluates its
  // argument on every render and discards the result -- harmless for a plain
  // object, but AudioStreamer used to open an AudioContext in its constructor,
  // so the page leaked one per render (one per second, from the metrics
  // sampler). Chrome caps concurrent contexts around six, and past that
  // `new AudioContext()` throws inside AudioRecorder.start(), where it is
  // caught and logged as a failed microphone: a later round would silently
  // have no audio until the page was reloaded.
  const audioStreamer = useRef(null);
  const audioRecorder = useRef(null);
  const wakeWord = useRef(null);
  const getStreamer = useCallback(() => {
    audioStreamer.current ??= new AudioStreamer(24000); // 24kHz out
    return audioStreamer.current;
  }, []);
  const getRecorder = useCallback(() => {
    audioRecorder.current ??= new AudioRecorder(16000); // 16kHz in
    return audioRecorder.current;
  }, []);
  const frameIntervalRef = useRef(1000); // Fallback only; server ships the real value (1 FPS)

  // Binary frame prefixes. The backend ships these in its `config` frame
  // (main.py AUDIO_PREFIX / JPEG_PREFIX); these values are only the fallback
  // for a server too old to send them. Do not hardcode them at the send site.
  const audioPrefixRef = useRef(1);
  const jpegPrefixRef = useRef(2);

  // Capture size and JPEG quality, also server-shipped. These values are the
  // fallback for a server that predates them; the measured defaults live in
  // main.py next to the numbers that chose them.
  const captureRef = useRef({ width: 640, height: 480, quality: 0.6 });

  // Live telemetry. Byte counters and timestamps live in a ref and are sampled
  // into state on a timer -- a session pushes ~125 audio packets a second, and
  // setState per packet would re-render the whole lock that often.
  //
  // The two latencies are the ones a browser can actually know:
  //   detect = last JPEG sent -> `match` frame (how long the video path takes
  //            to turn a hand into a decision)
  //   speak  = that match -> first model audio chunk after it (how long until
  //            you hear about it)
  // Neither is a socket round-trip; nothing here can measure one, so nothing
  // here claims to.
  // The rolling history is accumulated here rather than in the component that
  // draws it: this runs in a timer callback, where setState is fine, whereas a
  // component appending to its own state on every prop change is a cascading
  // render (and an eslint error).
  const [metrics, setMetrics] = useState({
    upKbps: 0,
    videoKbps: 0,
    audioKbps: 0,
    downKbps: 0,
    detectMs: null,
    speakMs: null,
    fps: 0,
    micOpen: false,
    micGated: false,
    micMode: "stream",
    upHistory: [],
    downHistory: [],
    // Token accounting, from usageMetadata on the raw ADK event. Measured
    // shape: one per model turn, promptTokenCount CUMULATIVE (the whole
    // context re-counted each turn), response count per-turn. So context is
    // the latest value and output is a running sum -- adding up the prompt
    // counts would multiply the context by the number of turns.
    contextTokens: 0,
    outputTokens: 0,
    tokensByModality: {},
    netMs: null,
    gateLevel: null,
    gateOpensAt: null,
  });
  // Any latency above this is a stale timestamp, not a slow model: the Live
  // session itself would have timed out long before.
  const MAX_PLAUSIBLE_MS = 10000;
  // `speak` is match -> the confirmation that follows it. A confirmation that
  // takes longer than this is not a confirmation of that match: the model has
  // gone quiet and the next chunk belongs to some later turn. Measuring across
  // that gap reported a real failure under a name that pointed at the wrong
  // thing -- the model was not slow to speak, it never spoke.
  const SPEAK_WINDOW_MS = 3000;
  const meterRef = useRef({
    video: 0,
    audio: 0,
    down: 0,
    frames: 0,
    since: null, // set on mount; performance.now() during render is impure
    lastFrameAt: null,
    lastMatchAt: null,
    detectMs: null,
    speakMs: null,
    micOpen: false,
    micGated: false,
    micMode: "stream",
    upHistory: [],
    downHistory: [],
    contextTokens: 0,
    outputTokens: 0,
    tokensByModality: {},
    netMs: null,
    pingAt: 0,
    gateLevel: null,
    gateOpensAt: null,
  });

  // Rolling event log. Bounded, and kept in a ref for the same reason the byte
  // counters are: a session produces these far faster than a render.
  // The live panels keep bounded views (40 samples, 80 events) because they
  // are drawn every second. The session record is the whole run, kept apart
  // so that trimming one never silently trims the other.
  const SESSION_MAX_SAMPLES = 3600; // an hour at 1Hz
  const SESSION_MAX_EVENTS = 2000;
  // startedAt is stamped on mount, not here: Date.now() during render is impure.
  const sessionRef = useRef({
    startedAt: null,
    config: null,
    samples: [],
    events: [],
  });

  const LOG_MAX = 80;
  const [events, setEvents] = useState([]);
  // Published for the in-app reviewer. A slice per tick so React sees a new
  // reference; the array holds at most an hour of 1Hz samples.
  const [sessionSamples, setSessionSamples] = useState([]);
  const logRef = useRef([]);
  const logDirty = useRef(false);
  const pushEvent = useCallback((kind, text) => {
    logRef.current = [
      ...logRef.current.slice(-(LOG_MAX - 1)),
      { t: Date.now(), kind, text },
    ];
    logDirty.current = true;
    const rec = sessionRef.current;
    if (rec.events.length < SESSION_MAX_EVENTS) {
      rec.events.push({ t: Date.now(), kind, text });
    }
  }, []);

  // Wipe the recorded run. Without this the record spans every round since page
  // load, and the review draws them as one continuous session with the gaps
  // between rounds flattened into it.
  const clearSession = useCallback(() => {
    const rec = sessionRef.current;
    rec.samples = [];
    rec.events = [];
    rec.startedAt = Date.now();
    logRef.current = [];
    setEvents([]);
    setSessionSamples([]);
    Object.assign(meterRef.current, {
      detectMs: null,
      speakMs: null,
      netMs: null,
      contextTokens: 0,
      outputTokens: 0,
      tokensByModality: {},
      upHistory: [],
      downHistory: [],
    });
  }, []);

  // Download the run as JSON. Rendered by scripts/telemetry_view.py.
  const saveSession = useCallback(() => {
    const rec = sessionRef.current;
    const blob = new Blob(
      [
        JSON.stringify(
          {
            started_at: rec.startedAt ?? Date.now(),
            ended_at: Date.now(),
            config: rec.config,
            samples: rec.samples,
            events: rec.events,
          },
          null,
          1,
        ),
      ],
      { type: "application/json" },
    );
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    const stamp = new Date(rec.startedAt)
      .toISOString()
      .replace(/[:.]/g, "-")
      .slice(0, 19);
    a.href = url;
    a.download = `telemetry-${stamp}.json`;
    document.body.appendChild(a);
    a.click();
    a.remove();
    // Revoked on the next tick: revoking synchronously can beat the download.
    setTimeout(() => URL.revokeObjectURL(url), 1000);
  }, []);

  useEffect(() => {
    const HISTORY = 40; // ~40s of 1Hz samples
    meterRef.current.since = performance.now();
    sessionRef.current.startedAt = Date.now();
    const id = setInterval(() => {
      const m = meterRef.current;
      const seconds = (performance.now() - m.since) / 1000;
      if (seconds < 0.25) return;
      const kbps = (bytes) => Math.round((bytes * 8) / seconds / 100) / 10;
      const up = kbps(m.video + m.audio);
      const down = kbps(m.down);
      m.upHistory = [...m.upHistory, up].slice(-HISTORY);
      m.downHistory = [...m.downHistory, down].slice(-HISTORY);
      setMetrics({
        upKbps: up,
        videoKbps: kbps(m.video),
        audioKbps: kbps(m.audio),
        downKbps: down,
        fps: Math.round((m.frames / seconds) * 10) / 10,
        detectMs: m.detectMs,
        speakMs: m.speakMs,
        micOpen: m.micOpen,
        micGated: m.micGated,
        micMode: m.micMode,
        upHistory: m.upHistory,
        downHistory: m.downHistory,
        contextTokens: m.contextTokens,
        outputTokens: m.outputTokens,
        tokensByModality: m.tokensByModality,
        netMs: m.netMs,
        gateLevel: m.gateLevel,
        gateOpensAt: m.gateOpensAt,
      });
      // Only record while a socket is actually open. The sampler runs
      // from mount, so without this the record filled with idle zeros
      // before any run and the review opened onto a flat empty chart.
      const live = ws.current?.readyState === WebSocket.OPEN;
      const rec = sessionRef.current;
      if (live && rec.samples.length < SESSION_MAX_SAMPLES) {
        rec.samples.push({
          t: Date.now(),
          up,
          down,
          video: kbps(m.video),
          audio: kbps(m.audio),
          fps: Math.round((m.frames / seconds) * 10) / 10,
          netMs: m.netMs,
          detectMs: m.detectMs,
          speakMs: m.speakMs,
          contextTokens: m.contextTokens,
          outputTokens: m.outputTokens,
          micOpen: m.micOpen,
          micGated: m.micGated,
        });
      }

      if (live) setSessionSamples(rec.samples.slice());

      // One probe per tick. Cheap (a few dozen bytes) and it keeps the
      // reading current without adding a second timer.
      if (ws.current?.readyState === WebSocket.OPEN) {
        ws.current.send(
          JSON.stringify({ type: "ping", sent_at: performance.now() }),
        );
      }

      // Same tick, so the log costs no extra renders.
      if (logDirty.current) {
        logDirty.current = false;
        setEvents(logRef.current);
      }
      Object.assign(m, {
        video: 0,
        audio: 0,
        down: 0,
        frames: 0,
        since: performance.now(),
      });
    }, 1000);
    return () => clearInterval(id);
  }, []);

  const stopStream = useCallback(() => {
    // Stop Video
    if (streamRef.current) {
      streamRef.current.getTracks().forEach((track) => track.stop());
      streamRef.current = null;
    }
    // Stop Audio
    audioRecorder.current?.stop();
    wakeWord.current?.stop();
    wakeWord.current = null;

    // Stop Frame Loop
    if (intervalRef.current) {
      clearTimeout(intervalRef.current);
      intervalRef.current = null;
    }
  }, []);

  const [config, setConfig] = useState({
    video_fps: 2,
    heartbeat_interval: 10,
  });

  // overrideUrl lets the caller hand in a URL minted this instant. Reading it
  // from the `url` prop instead would use the value captured when this
  // callback was created, so a caller that mints a fresh session id and
  // connects in the same tick would connect with the previous one -- which is
  // the whole bug this parameter exists to avoid.
  const connect = useCallback(
    (overrideUrl) => {
      if (ws.current?.readyState === WebSocket.OPEN) return;

      ws.current = new WebSocket(
        typeof overrideUrl === "string" ? overrideUrl : url,
      );

      ws.current.onopen = () => {
        console.log("Connected to Gemini Socket");
        setStatus("CONNECTED");

        // A new socket is a new conversation, so every per-session
        // measurement starts over. Without this the component -- which
        // stays mounted across rounds -- carried the previous round's
        // state: round 1 ends on a `match` whose audio never arrives
        // because the socket closes, leaving lastMatchAt set, and then
        // round 2's first audio chunk measured `speak` from round 1's
        // match, across the entire gap between rounds. It showed up as
        // speak going red on every run after the first.
        Object.assign(meterRef.current, {
          lastFrameAt: null,
          lastMatchAt: null,
          detectMs: null,
          speakMs: null,
          netMs: null,
          contextTokens: 0,
          outputTokens: 0,
          tokensByModality: {},
          gateLevel: null,
          gateOpensAt: null,
        });
      };

      ws.current.onclose = () => {
        console.log("Disconnected from Gemini Socket");
        setStatus("DISCONNECTED");
        stopStream();
        // The Live API can close a session with 1007 ("Request contains an
        // invalid argument") mid-run. It has not been reproducible outside a
        // real browser session -- eight scans over seventy seconds against this
        // backend never triggered it -- and ADK treats 1007 as fatal, so the
        // socket simply goes. Hand it to the caller rather than ending the
        // round: losing the conversation is bad, losing the demo is worse.
        if (!closingOnPurpose.current && onDroppedRef.current) {
          pushEvent("link", "session dropped; reconnecting");
          onDroppedRef.current();
        }
        closingOnPurpose.current = false;
      };

      ws.current.onerror = (err) => {
        console.error("Socket error:", err);
        setStatus("ERROR");
      };

      ws.current.onmessage = async (event) => {
        try {
          // Downlink volume. event.data is the JSON text frame; model audio
          // rides inside it as base64, which is 1 byte per character, so
          // length is a good enough proxy for bytes on the wire.
          if (typeof event.data === "string")
            meterRef.current.down += event.data.length;
          // console.log("Raw WS Frame:", event.data.slice(0, 200));
          const msg = JSON.parse(event.data);

          // Token accounting. Cumulative prompt, per-turn response --
          // see the note on the metrics state.
          const usage = msg.usageMetadata;
          if (usage) {
            const m = meterRef.current;
            m.contextTokens = usage.promptTokenCount ?? m.contextTokens;
            m.outputTokens +=
              usage.candidatesTokenCount ?? usage.responseTokenCount ?? 0;
            for (const d of usage.promptTokensDetails ?? []) {
              if (d.modality)
                m.tokensByModality[d.modality] = d.tokenCount ?? 0;
            }
          }

          // Trace: transcripts arrive on the raw event, not as frames of
          // their own, so they are read here rather than given a branch.
          if (msg.inputTranscription?.finished && msg.inputTranscription.text) {
            pushEvent("you", msg.inputTranscription.text.trim());
          }
          if (
            msg.outputTranscription?.finished &&
            msg.outputTranscription.text
          ) {
            pushEvent("scanner", msg.outputTranscription.text.trim());
          }
          // console.log("[useGeminiSocket] Received message from backend:", msg);

          if (msg.type === "pong") {
            if (typeof msg.sent_at === "number") {
              meterRef.current.netMs = Math.round(
                performance.now() - msg.sent_at,
              );
            }
            return;
          }

          // Handle configuration message
          if (msg.type === "config") {
            if (msg.frame_interval_ms) {
              console.log(
                `[DEBUG] SETTING FRAME INTERVAL TO ${msg.frame_interval_ms}ms (${msg.video_fps} FPS)`,
              );
              frameIntervalRef.current = msg.frame_interval_ms;
              setConfig({
                video_fps: msg.video_fps,
                heartbeat_interval: msg.heartbeat_interval,
              });
            }
            // Adopt the server's frame prefixes so the wire contract has
            // exactly one definition, on the server.
            if (typeof msg.audio_prefix === "number")
              audioPrefixRef.current = msg.audio_prefix;
            if (typeof msg.jpeg_prefix === "number")
              jpegPrefixRef.current = msg.jpeg_prefix;
            if (
              typeof msg.video_width === "number" &&
              typeof msg.video_height === "number"
            ) {
              captureRef.current = {
                width: msg.video_width,
                height: msg.video_height,
                // Server sends 0-100; canvas.toBlob wants 0-1.
                quality: (msg.jpeg_quality ?? 60) / 100,
              };
              console.log(
                `[DEBUG] CAPTURE ${msg.video_width}x${msg.video_height} q${msg.jpeg_quality}`,
              );
              sessionRef.current.config = {
                video_fps: msg.video_fps,
                video_width: msg.video_width,
                video_height: msg.video_height,
                jpeg_quality: msg.jpeg_quality,
              };
            }
            return;
          }

          // Detect mock server identification flag
          if (msg.mock === true) {
            setIsMock(true);
            return;
          }

          // Handle direct "match" message from backend
          if (msg.type === "match") {
            const count = msg.count || msg.digit;
            if (count !== undefined) {
              const val = parseInt(count, 10);
              const meter = meterRef.current;
              if (meter.lastFrameAt) {
                const dt = Math.round(performance.now() - meter.lastFrameAt);
                meter.detectMs = dt <= MAX_PLAUSIBLE_MS ? dt : null;
              }
              // Arm the speak measurement; the next audio chunk closes it.
              meter.lastMatchAt = performance.now();
              console.log(`[DEBUG] MATCH SIGNAL FROM BACKEND: ${val}`);
              if (onDigitDetectedRef.current) onDigitDetectedRef.current(val);
              pushEvent("match", `digit ${val}`);
            }
            return; // Skip further processing for this specific message
          }

          // Handle direct "system_error" message from backend
          if (msg.type === "system_error") {
            console.log(`[DEBUG] SYSTEM ERROR FROM BACKEND: ${msg.message}`);
            if (onSystemErrorRef.current) onSystemErrorRef.current(msg.message);
            pushEvent("error", "system error");
            return;
          }

          // Handle direct "heavy_metal" message from backend
          if (msg.type === "heavy_metal") {
            console.log(
              `[DEBUG] HEAVY METAL SIGNAL FROM BACKEND: ${msg.message}`,
            );
            if (onHeavyMetalRef.current) onHeavyMetalRef.current(msg.message);
            pushEvent("metal", "heavy metal");
            return;
          }

          // Barge-in. The model stops generating the moment the user
          // talks over it, but whatever it already sent is sitting in the
          // worklet's ring buffer and would keep playing. The Live API
          // docs are explicit: on interruption "stop playing audio and
          // clear queued playback". stop() posts {action:'clear'}, which
          // resets readIndex/writeIndex without tearing the node down, so
          // the next turn plays normally.
          if (msg.interrupted) {
            console.log("[DEBUG] Model interrupted; clearing playback queue");
            audioStreamer.current?.stop();
            pushEvent("barge-in", "playback cleared");
            return;
          }

          // Helper to extract parts from various possible event structures
          let parts = [];
          if (msg.serverContent?.modelTurn?.parts) {
            parts = msg.serverContent.modelTurn.parts;
          } else if (msg.content?.parts) {
            parts = msg.content.parts;
          }

          if (parts.length > 0) {
            // console.log(`[useGeminiSocket] Processing ${parts.length} parts`);
            parts.forEach((part) => {
              // Tool calls are logged only. The digit signal arrives on
              // the `match` channel above, which is where the server's
              // 1.5s dedup lives. Acting on functionCall here too made
              // every detection fire onDigitDetected twice, because the
              // server sends the match frame AND the raw event that
              // still contains the same call.
              if (part.functionCall) {
                console.log("[DEBUG] Tool Call Detected:", part.functionCall);
                pushEvent(
                  "tool",
                  `${part.functionCall.name}(${JSON.stringify(part.functionCall.args ?? {})})`,
                );
              }

              // Handle Audio (inlineData)
              if (part.inlineData && part.inlineData.data) {
                // console.log(`[useGeminiSocket] Found inlineData: ${part.inlineData.data.length} chars`);
                const meter = meterRef.current;
                if (meter.lastMatchAt) {
                  const dt = Math.round(performance.now() - meter.lastMatchAt);
                  meter.speakMs = dt <= SPEAK_WINDOW_MS ? dt : null;
                  meter.lastMatchAt = null; // first chunk only
                }
                // Resume context if needed (autoplay policy)
                const streamer = getStreamer();
                streamer.resume();
                streamer.addPCM16(part.inlineData.data);
              }

              // Handle Text (transcript)
              if (part.text) {
                console.log(`[DEBUG] Gemini said: ${part.text}`);
              }
            });
          }
        } catch (e) {
          console.error("Failed to parse message", e, event.data.slice(0, 100));
        }
      };
    },
    [url, stopStream, pushEvent, getStreamer],
  );

  const startStream = useCallback(
    async (videoElement) => {
      try {
        console.log("[DEBUG] Starting stream...");
        // 1. Start Video Stream
        const stream = await navigator.mediaDevices.getUserMedia({
          video: true,
        });
        videoElement.srcObject = stream;
        streamRef.current = stream;
        await videoElement.play();
        console.log("[DEBUG] Video stream started");

        // 2. Microphone.
        //
        // Its only job in this demo is to catch the word "scan". Streaming it
        // to the Live API to do that costs 256 kbit/s of raw PCM -- about two
        // thirds of the uplink -- and continuous audio is also what stops the
        // model taking turns: measured 0/5 with speech in the room, against
        // 5/5 for the identical prompts delivered as text.
        //
        // So by default the audio stays in the browser: the Web Speech API
        // listens for the command and we send the same text frame the offline
        // harness sends. ?mic=stream forces the old behaviour, ?mic=off sends
        // nothing. Anywhere SpeechRecognition is missing falls back to
        // streaming rather than leaving the demo unable to hear anything.
        const requested = new URLSearchParams(window.location.search).get(
          "mic",
        );
        const micMode = requested || (wakeWordSupported() ? "wake" : "stream");
        meterRef.current.micMode = micMode;
        console.log(`[DEBUG] MIC MODE: ${micMode}`);

        if (micMode === "wake" && wakeWordSupported()) {
          try {
            wakeWord.current = new WakeWordListener(undefined, {
              onCommand: (heard) => {
                if (ws.current?.readyState !== WebSocket.OPEN) return;
                ws.current.send(JSON.stringify({ type: "text", text: "scan" }));
                pushEvent("you", `"${heard}" → scan (local)`);
              },
            });
            wakeWord.current.start();
          } catch (wakeErr) {
            console.error(
              "Wake word unavailable, streaming audio instead:",
              wakeErr,
            );
            meterRef.current.micMode = "stream";
          }
        }

        if (meterRef.current.micMode === "stream") {
          try {
            let packetCount = 0;
            // The gate decides what reaches the model; show it so a demo can
            // see whether the mic is actually transmitting.
            const recorder = getRecorder();
            recorder.onGateDebug = (d) => {
              meterRef.current.gateLevel = d.level;
              meterRef.current.gateOpensAt = d.opensAt;
            };
            recorder.onGateChange = (open) => {
              meterRef.current.micOpen = open;
              // Whether a gate is in play at all, which is a different
              // question from whether it is currently open.
              meterRef.current.micGated = Boolean(recorder.gateEnabled);
              pushEvent("mic", open ? "open" : "gated");
            };
            await recorder.start((pcmBuffer) => {
              if (ws.current?.readyState === WebSocket.OPEN) {
                packetCount++;
                if (packetCount % 50 === 0) {
                  console.log(
                    `[useGeminiSocket] Sending Audio Packet #${packetCount}`,
                  );
                }
                const packet = new Uint8Array(pcmBuffer.byteLength + 1);
                packet[0] = audioPrefixRef.current;
                packet.set(new Uint8Array(pcmBuffer), 1);
                ws.current.send(packet);
                meterRef.current.audio += packet.byteLength;
              }
            });

            // Tell the server the rate the browser actually gave us. Chrome
            // honours the requested 16kHz; engines that fall back to the
            // hardware rate would otherwise have 48kHz samples labelled as
            // 16kHz, which the model hears as unintelligible fast speech.
            const actualRate = recorder.actualSampleRate;
            if (ws.current?.readyState === WebSocket.OPEN && actualRate) {
              ws.current.send(
                JSON.stringify({
                  type: "audio_config",
                  sample_rate: actualRate,
                }),
              );
            }
            console.log(
              `[DEBUG] Microphone recording started (BINARY PROTOCOL) at ${actualRate} Hz`,
            );
          } catch (authErr) {
            console.error("Microphone access denied or error:", authErr);
          }
        }

        // 3. Setup Video Frame Capture loop (Precise 2 FPS)
        const canvas = document.createElement("canvas");
        const ctx = canvas.getContext("2d");
        // Read once per stream start: the config frame has already arrived.
        // Pixels are what the uplink costs -- 640x480 measured 128 kbit/s,
        // 480x360 measured 77, both 5/5 on scan accuracy.
        const { width, height, quality } = captureRef.current;
        canvas.width = width;
        canvas.height = height;

        let frameCount = 0;

        // A timer, not requestAnimationFrame: rAF stops dead in a hidden tab,
        // so tabbing away killed video while the mic kept streaming and
        // detection silently stopped. Timers only clamp to ~1s in background.
        const captureFrame = () => {
          if (ws.current?.readyState === WebSocket.OPEN) {
            ctx.drawImage(videoElement, 0, 0, width, height);

            // Optimized: toBlob is async and doesn't block the main thread like toDataURL
            canvas.toBlob(
              (blob) => {
                if (!blob) return;
                blob.arrayBuffer().then((buffer) => {
                  frameCount++;
                  if (frameCount % 10 === 0) {
                    console.log(`[DEBUG] Sending binary frame #${frameCount}`);
                  }
                  const packet = new Uint8Array(buffer.byteLength + 1);
                  packet[0] = jpegPrefixRef.current;
                  packet.set(new Uint8Array(buffer), 1);
                  if (ws.current?.readyState === WebSocket.OPEN) {
                    ws.current.send(packet);
                    const meter = meterRef.current;
                    meter.video += packet.byteLength;
                    meter.frames += 1;
                    meter.lastFrameAt = performance.now();
                  }
                });
              },
              "image/jpeg",
              quality,
            );
          }

          // stopStream() nulls the ref; don't resurrect a cancelled loop.
          if (intervalRef.current !== null) {
            intervalRef.current = setTimeout(
              captureFrame,
              frameIntervalRef.current,
            );
          }
        };

        intervalRef.current = setTimeout(
          captureFrame,
          frameIntervalRef.current,
        );
        console.log("[DEBUG] Video capture loop started (timer)");
      } catch (err) {
        console.error("Error accessing camera:", err);
      }
    },
    [pushEvent, getRecorder],
  );

  useEffect(() => {
    return () => {
      stopStream();
      if (ws.current) ws.current.close();
    };
  }, [stopStream]);

  const disconnect = useCallback(() => {
    closingOnPurpose.current = true;
    if (ws.current) {
      ws.current.close();
      ws.current = null;
    }
    setStatus("DISCONNECTED");
    stopStream();
  }, [stopStream]);

  return {
    status,
    isMock,
    config,
    metrics,
    events,
    sessionSamples,
    saveSession,
    clearSession,
    connect,
    disconnect,
    startStream,
    stopStream,
  };
}
