export class AudioStreamer {
    // No AudioContext here. Constructing one in the constructor made this class
    // expensive to *create*, and it was being created on every render: the hook
    // held it in a useRef, whose argument is evaluated every time regardless of
    // whether the value is kept. At one render per second (the metrics sampler)
    // that leaked one AudioContext per second, all discarded, none closed.
    // Chrome caps concurrent contexts around six, after which construction
    // throws -- inside AudioRecorder.start(), where it is caught and logged as a
    // failed microphone. A later round would silently have no audio, and only a
    // page reload cleared it.
    //
    // Deferring to first use also suits the autoplay policy: by then a user
    // gesture has happened, so the context starts running rather than suspended.
    constructor(sampleRate = 24000) {
        this.context = null;
        this.sampleRate = sampleRate;
        this.workletNode = null;
        this.initialized = false;
    }

    async ensureInitialized() {
        if (this.initialized) return;
        if (this.initializingPromise) return this.initializingPromise;

        this.initializingPromise = (async () => {
            try {
                this.context = new (window.AudioContext || window.webkitAudioContext)({
                    sampleRate: this.sampleRate,
                });
                await this.context.audioWorklet.addModule('/audio-processor.js');
                this.workletNode = new AudioWorkletNode(this.context, 'audio-processor');
                this.workletNode.connect(this.context.destination);
                this.initialized = true;
                console.log("[AudioStreamer] AudioWorklet initialized.");
            } catch (e) {
                console.error("[AudioStreamer] Failed to initialize AudioWorklet:", e);
                this.initializingPromise = null;
            }
        })();

        return this.initializingPromise;
    }

    async addPCM16(base64Data) {
        try {
            await this.ensureInitialized();

            const cleaned = base64Data.replace(/-/g, '+').replace(/_/g, '/');
            const raw = atob(cleaned);
            const rawLength = raw.length;
            const array = new Int16Array(new ArrayBuffer(rawLength));

            for (let i = 0; i < rawLength / 2; i++) {
                const lower = raw.charCodeAt(i * 2);
                const upper = raw.charCodeAt(i * 2 + 1);
                let sample = (upper << 8) | lower;
                if (sample & 0x8000) {
                    sample = sample - 0x10000;
                }
                array[i] = sample;
            }

            const float32Data = new Float32Array(array.length);
            for (let i = 0; i < array.length; i++) {
                float32Data[i] = array[i] / 32768.0;
            }

            if (this.context && this.context.state === 'suspended') {
                await this.context.resume();
            }

            if (this.workletNode) {
                this.workletNode.port.postMessage({
                    action: 'play',
                    audio: float32Data
                });
            }
        } catch (e) {
            console.error('[AudioStreamer] Error in addPCM16:', e);
        }
    }

    stop() {
        if (this.workletNode) {
            this.workletNode.port.postMessage({ action: 'clear' });
        }
    }

    resume() {
        // Before first use there is no context yet, and nothing to resume:
        // addPCM16() builds one when audio actually arrives.
        if (this.context && this.context.state === 'suspended') {
            this.context.resume();
        }
    }
}
