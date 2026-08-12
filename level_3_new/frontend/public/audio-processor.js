const MAX_BUFFER_SIZE = 48000 * 2; // 2 seconds of 48kHz audio (safe upper bound)

class AudioProcessor extends AudioWorkletProcessor {
  constructor() {
    super();
    // Pre-allocate circular buffer
    this.buffer = new Float32Array(MAX_BUFFER_SIZE);
    this.readIndex = 0;
    this.writeIndex = 0;
    this.bufferedSamples = 0;

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

  process(inputs, outputs) {
    // 1. Handle Recording (Input -> Main Thread)
    const input = inputs[0];
    if (input && input.length > 0) {
      const inputChannel = input[0];
      if (inputChannel.length > 0) {
        // Zero-copy transfer of PCM16
        const pcm16Buffer = this.floatTo16BitPCM(inputChannel);
        this.port.postMessage({
          action: 'record',
          audio: pcm16Buffer
        }, [pcm16Buffer]); 
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
