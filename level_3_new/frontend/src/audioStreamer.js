export class AudioStreamer {
    constructor(sampleRate = 24000) {
        this.context = new (window.AudioContext || window.webkitAudioContext)({
            sampleRate: sampleRate,
        });
        this.sampleRate = sampleRate;
        this.workletNode = null;
        this.initialized = false;
    }

    async ensureInitialized() {
        if (this.initialized) return;
        if (this.initializingPromise) return this.initializingPromise;
        
        this.initializingPromise = (async () => {
            try {
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

            if (this.context.state === 'suspended') {
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
        if (this.context.state === 'suspended') {
            this.context.resume();
        }
    }
}
