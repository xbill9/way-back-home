// Local wake-word listening, so the microphone never reaches the Live API.
//
// The microphone's entire job in this demo is to catch the word "scan". Sending
// it to Gemini to do that costs 256 kbit/s of raw PCM -- about two thirds of the
// uplink -- and it is also the single biggest source of unreliability measured
// in this project: continuous audio reads to the Live API as a user turn that
// never ends, so the model stops taking turns of its own. Speech in the room
// scored 0/5 with the scanner answering nothing at all, while the same prompts
// delivered as text scored 5/5 at sub-second latency.
//
// So the audio stays in the browser. The Web Speech API listens for the command
// and the socket sends the same `{"type":"text"}` frame the offline harness
// sends -- the configuration already measured as the reliable one.
//
// Trade-offs worth knowing:
//   * Chrome and Edge only (webkitSpeechRecognition). isSupported() is false
//     elsewhere, and the caller falls back to streaming audio.
//   * Chrome's implementation sends audio to Google's speech service. It is not
//     billed to the Live API key, but it is not on-device either.
//   * Recognition stops itself on silence, so it is restarted continuously.

export function isSupported() {
    return typeof window !== 'undefined' &&
        Boolean(window.SpeechRecognition || window.webkitSpeechRecognition);
}

export class WakeWordListener {
    // `phrases` are matched as substrings of the transcript, lowercased. "scan"
    // catches "scan", "scan it", and the mis-hearings that show up constantly in
    // real transcripts ("Stan", "scanned").
    constructor(phrases = ['scan', 'stan', 'skin'], { onCommand, onHeard } = {}) {
        this.phrases = phrases;
        this.onCommand = onCommand;
        this.onHeard = onHeard;
        this.recognition = null;
        this.running = false;
        this.lastFiredAt = 0;
    }

    start() {
        const Impl = window.SpeechRecognition || window.webkitSpeechRecognition;
        if (!Impl) throw new Error('SpeechRecognition unavailable');

        this.running = true;
        const recognition = new Impl();
        recognition.continuous = true;
        recognition.interimResults = true;   // fire on the word, not the pause
        recognition.lang = 'en-US';

        recognition.onresult = (event) => {
            for (let i = event.resultIndex; i < event.results.length; i++) {
                const text = (event.results[i][0]?.transcript || '').toLowerCase().trim();
                if (!text) continue;
                if (this.onHeard) this.onHeard(text);
                if (!this.phrases.some((p) => text.includes(p))) continue;

                // One command per second: interim results repeat the same words
                // as they are refined, and each refinement would otherwise be a
                // separate scan request.
                const now = performance.now();
                if (now - this.lastFiredAt < 1000) continue;
                this.lastFiredAt = now;
                if (this.onCommand) this.onCommand(text);
            }
        };

        // It stops on its own after silence, and on most errors. Restart unless
        // we were the ones who stopped it. 'not-allowed' is permission and will
        // never recover, so it is the one error that ends the loop.
        recognition.onend = () => { if (this.running) this._restart(); };
        recognition.onerror = (e) => {
            if (e.error === 'not-allowed' || e.error === 'service-not-allowed') {
                console.error('[WakeWord] permission denied; falling silent');
                this.running = false;
            }
        };

        this.recognition = recognition;
        recognition.start();
        console.log(`[WakeWord] listening locally for ${this.phrases.join(' / ')}`);
    }

    _restart() {
        // A short delay: restarting synchronously inside onend throws
        // InvalidStateError in Chrome.
        setTimeout(() => {
            if (!this.running || !this.recognition) return;
            try {
                this.recognition.start();
            } catch {
                /* already starting; the next onend will retry */
            }
        }, 150);
    }

    stop() {
        this.running = false;
        if (this.recognition) {
            try {
                this.recognition.stop();
            } catch {
                /* not started */
            }
            this.recognition = null;
        }
    }
}
