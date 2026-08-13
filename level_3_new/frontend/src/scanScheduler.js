// Decides *when* a scan request reaches the model.
//
// Sending one while the model is mid-reply interrupts it: measured with six
// scans at 1.5s spacing, five interruptions, and only four of six replies
// finished. Nothing broke -- every scan still produced a detection -- but the
// scanner sounds clipped, cut off mid-sentence, which reads as a fault.
//
// Dropping the request would fix the audio and lose the scan. Delaying it by a
// fixed amount would trade the clipping for lag. So it is held only while the
// model is actually speaking and released the moment `turnComplete` arrives:
// during silence a scan goes straight out, and during a reply it waits about as
// long as the reply has left (~1s for "Three digits.").
//
// Repeated requests while speaking collapse into one. Saying "scan scan scan"
// over a reply should mean one scan when it finishes, not three queued.
export class ScanScheduler {
    constructor({ send, now = () => performance.now(), maxHoldMs = 2000 } = {}) {
        this.send = send;
        this.now = now;
        // Backstop: if turnComplete never arrives, a held request must not be
        // stranded. Two seconds is longer than any reply this agent produces.
        this.maxHoldMs = maxHoldMs;
        this.speaking = false;
        this.pending = false;
        this.pendingSince = 0;
    }

    // The wake word fired.
    request() {
        if (!this.speaking) {
            this.send();
            return 'sent';
        }
        if (!this.pending) {
            this.pending = true;
            this.pendingSince = this.now();
        }
        return 'held';
    }

    // A chunk of model audio arrived.
    onAudio() {
        this.speaking = true;
    }

    // The model finished its turn.
    onTurnComplete() {
        this.speaking = false;
        return this._flush();
    }

    // Called periodically; only enforces the backstop.
    tick() {
        if (this.pending && this.now() - this.pendingSince >= this.maxHoldMs) {
            return this._flush();
        }
        return null;
    }

    // A new session starts with nothing held and nobody speaking.
    reset() {
        this.speaking = false;
        this.pending = false;
    }

    _flush() {
        if (!this.pending) return null;
        this.pending = false;
        this.send();
        return 'sent';
    }
}
