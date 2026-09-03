
export class AudioRecorder {
    constructor(sampleRate = 16000) {
        this.sampleRate = sampleRate;
        // What the browser actually granted, which is not always what we asked
        // for. Read this rather than `sampleRate` when labelling the audio.
        this.actualSampleRate = null;
        this.stream = null;
        this.audioContext = null;
        this.source = null;
        this.processor = null;
        this.onAudioData = null;
        // Called with true/false as the near-field gate opens and closes.
        this.onGateChange = null;
        // Live level/threshold, for the readout that tuning needs.
        this.onGateDebug = null;
        // Whether a gate is in play at all -- distinct from whether it is open.
        this.gateEnabled = false;
    }

    async start(onAudioData) {
        this.onAudioData = onAudioData;

        try {
            console.log("[AudioRecorder] Requesting microphone access...");
            this.stream = await navigator.mediaDevices.getUserMedia({
                audio: {
                    echoCancellation: true,
                    noiseSuppression: true,
                    autoGainControl: true
                }
            });
            console.log("[AudioRecorder] Microphone access granted.");

            this.audioContext = new (window.AudioContext || window.webkitAudioContext)({
                sampleRate: this.sampleRate
            });
            console.log(`[AudioRecorder] AudioContext created. State: ${this.audioContext.state}, Rate: ${this.audioContext.sampleRate}`);

            if (this.audioContext.state === 'suspended') {
                console.log("[AudioRecorder] Context suspended. Resuming...");
                await this.audioContext.resume();
                console.log(`[AudioRecorder] Context resumed. New State: ${this.audioContext.state}`);
            }

            // A browser is free to ignore the requested rate and hand back the
            // hardware rate. Silent failure otherwise: the model just stops
            // understanding speech.
            this.actualSampleRate = this.audioContext.sampleRate;
            if (this.actualSampleRate !== this.sampleRate) {
                console.warn(
                    `[AudioRecorder] Requested ${this.sampleRate} Hz but got ${this.actualSampleRate} Hz. ` +
                    `Reporting the real rate to the server so audio is not mislabelled.`
                );
            }

            this.source = this.audioContext.createMediaStreamSource(this.stream);
            console.log("[AudioRecorder] Source reached.");

            // Use AudioWorklet for off-main-thread processing
            await this.audioContext.audioWorklet.addModule('/audio-processor.js');
            // The near-field gate keeps room noise off the wire. It is most of
            // the bandwidth story -- the microphone is ~77% of the uplink and
            // sends 256 kbit/s of raw PCM whether or not anyone is talking --
            // and it is the fix for speech in the room scoring 0/5.
            //
            // The rewrite caps the noise floor so the open threshold is bounded
            // below ordinary speech, making the worst case "always open" rather
            // than "never opens".
            //
            // OFF by default, for the second time. Two rewrites have now been
            // reported broken on a real microphone while passing every offline
            // check, so it stays opt-in until numbers from a real room say the
            // thresholds are right. ?gate=1 enables it, and doing so also streams
            // the live level/threshold to the telemetry panel -- that readout is
            // the whole point of turning it on now.
            const params = new URLSearchParams(window.location.search);
            const gate = params.get('gate') === '1';
            const debug = gate;
            this.workletNode = new AudioWorkletNode(this.audioContext, 'audio-processor', {
                processorOptions: { gate, debug }
            });
            console.log(`[AudioRecorder] AudioWorkletNode created (near-field gate ${gate ? 'on' : 'OFF'}).`);

            // Say so explicitly when gating is off, or the panel has no way to
            // know: every gate message is emitted from inside the gate itself,
            // so with it disabled nothing ever reports the mic as transmitting
            // and the readout sits on its initial "gated" -- which is exactly
            // backwards, since an ungated mic sends everything.
            this.gateEnabled = gate;
            if (this.onGateChange) this.onGateChange(true);

            this.workletNode.port.onmessage = (event) => {
                if (event.data.action === 'record') {
                    // Audio is now pre-converted to PCM16 binary in the worklet thread
                    if (this.onAudioData) {
                        this.onAudioData(event.data.audio);
                    }
                } else if (event.data.action === 'gate') {
                    if (this.onGateChange) this.onGateChange(event.data.open);
                } else if (event.data.action === 'gate-debug') {
                    const d = event.data;
                    if (this.onGateDebug) this.onGateDebug(d);
                    console.log(
                        `[gate] level ${d.level} vs opens-at ${d.opensAt} ` +
                        `(floor ${d.floor}) -- ${d.open ? 'OPEN' : 'shut'}`
                    );
                }
            };

            this.source.connect(this.workletNode);
            this.workletNode.connect(this.audioContext.destination);
            console.log("[AudioRecorder] Connected graph with AudioWorklet.");

        } catch (error) {
            console.error("[AudioRecorder] Error starting audio recording:", error);
            throw error;
        }
    }

    stop() {
        if (this.stream) {
            this.stream.getTracks().forEach(track => track.stop());
            this.stream = null;
        }
        if (this.source) {
            this.source.disconnect();
            this.source = null;
        }
        if (this.workletNode) {
            this.workletNode.disconnect();
            this.workletNode = null;
        }
        if (this.audioContext) {
            this.audioContext.close();
            this.audioContext = null;
        }
    }

    // This method is now kept only for backward compatibility or legacy use
    floatTo16BitPCM(input) {
        const output = new Int16Array(input.length);
        for (let i = 0; i < input.length; i++) {
            const s = Math.max(-1, Math.min(1, input[i]));
            output[i] = s < 0 ? s * 0x8000 : s * 0x7FFF;
        }
        return output.buffer;
    }

    arrayBufferToBase64(buffer) {
        let binary = '';
        const bytes = new Uint8Array(buffer);
        const len = bytes.byteLength;
        for (let i = 0; i < len; i++) {
            binary += String.fromCharCode(bytes[i]);
        }
        return window.btoa(binary);
    }
}
