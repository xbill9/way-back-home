const MAX_BUFFER_SIZE = 48000 * 2; // 2 seconds of 48kHz audio (safe upper bound)

// Near-field gate. Speech in the room measured 0/5 with the scanner answering
// nothing at all (scripts/scan_accuracy.py --noise chatter): continuous sound
// reads as a user turn that never ends, so the model never takes one. Tuning
// the server's VAD sensitivity only reached 1/5, so the fix is to stop sending
// the noise -- which also drops the uplink, since raw 16kHz PCM is ~78% of it.
//
// The discriminator is that room noise is *steady* while speech is intermittent,
// so the floor tracks the running minimum: whatever level the room never drops
// below is the room. Speech from a microphone a hand's width away sits several
// times above that; voices across the room do not.
//
// Two earlier versions of this got it wrong, and both failures are designed
// against here:
//
//   1. A slow-rising average floor. Steady chatter sat above it forever and
//      400 of 400 blocks went out -- it gated nothing.
//   2. An unbounded minimum-tracking floor. It passed every synthetic test and
//      then, on a real microphone, held itself shut for an entire round: the
//      user could not talk to the scanner at all.
//
// The second failure is the one that matters, because a gate that stops you
// talking is worse than no gate. So the floor is now CAPPED. The open threshold
// is at most GATE_FLOOR_MAX * GATE_OPEN_RATIO, which is below ordinary speech,
// which means speech always gets through no matter what the room does. If the
// room is louder than the cap the gate simply stays open -- degrading to the
// old always-on behaviour rather than to silence. Fail-open by construction.
//
// The floor itself is a windowed minimum (bucketed over GATE_WINDOW_MS) rather
// than a decay, so it cannot drift somewhere it can never come back from.
const GATE_OPEN_RATIO = 3.0;    // speech is this much above the floor
const GATE_CLOSE_RATIO = 1.8;   // hysteresis, so it does not chatter open/shut
const GATE_FLOOR_MIN = 0.004;   // silence is never "speech"
const GATE_FLOOR_MAX = 0.02;    // ...and the bar never rises above 0.06 RMS
const GATE_WINDOW_MS = 3000;    // how far back the "quietest recent moment" looks
const GATE_BUCKET_MS = 500;     // resolution of that window
const GATE_WARMUP_MS = 1000;    // send everything while the floor is still forming
const GATE_HANGOVER_MS = 300;   // keep sending after speech drops, for trailing
                                // words. Kept short on purpose: every extra ms
                                // here is real room noise the server's VAD hears
                                // instead of the silence that ends your turn.
const GATE_PREROLL_MS = 250;    // ...and backdate this much, so onsets survive
const GATE_SILENCE_TAIL_MS = 400; // zeros after closing, so the server's VAD hears
                                  // an ended turn instead of a stream that stopped
                                  // -- a stopped stream is only resolved by timeout,
                                  // which is felt as lag

class AudioProcessor extends AudioWorkletProcessor {
  constructor(options) {
    super();
    // Pre-allocate circular buffer
    this.buffer = new Float32Array(MAX_BUFFER_SIZE);
    this.readIndex = 0;
    this.writeIndex = 0;
    this.bufferedSamples = 0;

    // Gating is opt-in: this same processor class is also used for playback,
    // where there is no input at all, and a caller may want the raw stream.
    this.gateEnabled = Boolean(options?.processorOptions?.gate);
    this.gateDebug = Boolean(options?.processorOptions?.debug);
    this.noiseFloor = GATE_FLOOR_MIN;
    this.gateOpen = false;
    this.hangoverUntil = 0;
    this.debugAt = 0;
    // Windowed minimum: one running minimum per bucket, floor = min of buckets.
    this.buckets = [];
    this.bucketMin = Infinity;
    this.bucketEndsAt = 0;
    this.startedAt = null;
    this.silenceTail = 0; // blocks of zeros still owed to the server's VAD
    this.blocksPerMs = sampleRate / 128 / 1000;
    this.preroll = [];
    this.prerollBlocks = Math.max(
      1, Math.round((GATE_PREROLL_MS / 1000) * sampleRate / 128)
    );

    this.port.onmessage = (event) => {
      if (event.data.action === 'play') {
        const newData = event.data.audio;
        this.writeToBuffer(newData);
      } else if (event.data.action === 'clear') {
        this.readIndex = 0;
        this.writeIndex = 0;
        this.bufferedSamples = 0;
      }
    };
  }

  writeToBuffer(data) {
    const len = data.length;
    // If we're about to overflow, we just drop oldest data (or just let it wrap)
    // In a live stream, it's better to stay current.
    for (let i = 0; i < len; i++) {
      this.buffer[this.writeIndex] = data[i];
      this.writeIndex = (this.writeIndex + 1) % MAX_BUFFER_SIZE;
    }
    this.bufferedSamples = Math.min(MAX_BUFFER_SIZE, this.bufferedSamples + len);
  }

  floatTo16BitPCM(input) {
    const output = new Int16Array(input.length);
    for (let i = 0; i < input.length; i++) {
      const s = Math.max(-1, Math.min(1, input[i]));
      output[i] = s < 0 ? s * 0x8000 : s * 0x7FFF;
    }
    return output.buffer;
  }

  // RMS of one render block, used as the gate's input signal.
  static rms(block) {
    let sum = 0;
    for (let i = 0; i < block.length; i++) sum += block[i] * block[i];
    return Math.sqrt(sum / block.length);
  }

  // Returns true while the user is judged to be speaking. Everything sent to
  // the model passes through here when gating is on.
  shouldSend(block) {
    const level = AudioProcessor.rms(block);
    const now = currentTime * 1000;

    if (this.startedAt === null) {
      this.startedAt = now;
      this.bucketEndsAt = now + GATE_BUCKET_MS;
    }

    // Floor = quietest moment in the last GATE_WINDOW_MS, bucketed. A window
    // rather than a decay: a windowed minimum always recovers, so the floor can
    // never end up somewhere it cannot come back from.
    this.bucketMin = Math.min(this.bucketMin, level);
    if (now >= this.bucketEndsAt) {
      this.buckets.push(this.bucketMin);
      while (this.buckets.length > Math.ceil(GATE_WINDOW_MS / GATE_BUCKET_MS)) {
        this.buckets.shift();
      }
      this.bucketMin = Infinity;
      this.bucketEndsAt = now + GATE_BUCKET_MS;
    }
    const windowed = Math.min(this.bucketMin, ...this.buckets);
    // The cap is the safety property: opens-at can never exceed
    // GATE_FLOOR_MAX * GATE_OPEN_RATIO, which is below ordinary speech.
    this.noiseFloor = Math.min(GATE_FLOOR_MAX, Math.max(GATE_FLOOR_MIN, windowed));

    // Send everything until the window has some history. Starting shut would
    // swallow whatever the user says in the first second of a round.
    if (now - this.startedAt < GATE_WARMUP_MS) {
      if (!this.gateOpen) {
        this.gateOpen = true;
        this.port.postMessage({ action: 'gate', open: true });
      }
      this.hangoverUntil = now + GATE_HANGOVER_MS;
      return true;
    }

    // Twice a second, report what the gate is actually comparing. Without this
    // a stuck-shut gate is indistinguishable from a dead microphone.
    if (this.gateDebug && now - this.debugAt > 500) {
      this.debugAt = now;
      this.port.postMessage({
        action: 'gate-debug',
        level: Number(level.toFixed(5)),
        floor: Number(this.noiseFloor.toFixed(5)),
        opensAt: Number((this.noiseFloor * GATE_OPEN_RATIO).toFixed(5)),
        open: this.gateOpen,
      });
    }

    if (level > this.noiseFloor * GATE_OPEN_RATIO) {
      this.hangoverUntil = now + GATE_HANGOVER_MS;
      if (!this.gateOpen) {
        this.gateOpen = true;
        this.port.postMessage({ action: 'gate', open: true });
      }
    } else if (this.gateOpen && level < this.noiseFloor * GATE_CLOSE_RATIO && now > this.hangoverUntil) {
      this.gateOpen = false;
      // Owe the server a short run of true silence, so it can end the turn now
      // rather than waiting out a stream that simply stopped arriving.
      this.silenceTail = Math.round(GATE_SILENCE_TAIL_MS * this.blocksPerMs);
      this.port.postMessage({ action: 'gate', open: false });
    }
    return this.gateOpen;
  }

  process(inputs, outputs) {
    // 1. Handle Recording (Input -> Main Thread)
    const input = inputs[0];
    if (input && input.length > 0) {
      const inputChannel = input[0];
      if (inputChannel.length > 0) {
        let send = true;
        if (this.gateEnabled) {
          const wasOpen = this.gateOpen;
          send = this.shouldSend(inputChannel);
          if (send && !wasOpen) {
            // Gate just opened: flush the pre-roll first, so the model hears the
            // start of the word rather than joining midway through it.
            for (const held of this.preroll) {
              const buf = this.floatTo16BitPCM(held);
              this.port.postMessage({ action: 'record', audio: buf }, [buf]);
            }
          }
          if (!send) {
            // Keep the most recent blocks around to become that pre-roll.
            this.preroll.push(inputChannel.slice());
            if (this.preroll.length > this.prerollBlocks) this.preroll.shift();

            // Pay down the silence owed to the server's VAD before going quiet.
            if (this.silenceTail > 0) {
              this.silenceTail--;
              const zeros = this.floatTo16BitPCM(new Float32Array(inputChannel.length));
              this.port.postMessage({ action: 'record', audio: zeros }, [zeros]);
            }
          } else {
            this.preroll.length = 0;
            this.silenceTail = 0;
          }
        }

        if (send) {
          // Zero-copy transfer of PCM16
          const pcm16Buffer = this.floatTo16BitPCM(inputChannel);
          this.port.postMessage({
            action: 'record',
            audio: pcm16Buffer
          }, [pcm16Buffer]);
        }
      }
    }

    // 2. Handle Playback (Circular Buffer -> Output)
    const output = outputs[0];
    if (output && output.length > 0) {
      const outputChannel = output[0];
      const length = outputChannel.length;

      if (this.bufferedSamples >= length) {
        for (let i = 0; i < length; i++) {
          outputChannel[i] = this.buffer[this.readIndex];
          this.readIndex = (this.readIndex + 1) % MAX_BUFFER_SIZE;
        }
        this.bufferedSamples -= length;
      } else {
        // Underrun: play what we have, then silence
        for (let i = 0; i < this.bufferedSamples; i++) {
          outputChannel[i] = this.buffer[this.readIndex];
          this.readIndex = (this.readIndex + 1) % MAX_BUFFER_SIZE;
        }
        outputChannel.fill(0, this.bufferedSamples);
        this.bufferedSamples = 0;
      }
    }

    return true;
  }
}

registerProcessor('audio-processor', AudioProcessor);
