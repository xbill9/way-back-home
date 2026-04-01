class AudioProcessor extends AudioWorkletProcessor {
  constructor() {
    super();
    this.buffer = new Float32Array(0);
    this.port.onmessage = (event) => {
      if (event.data.action === 'play') {
        const newData = event.data.audio;
        const newBuffer = new Float32Array(this.buffer.length + newData.length);
        newBuffer.set(this.buffer);
        newBuffer.set(newData, this.buffer.length);
        this.buffer = newBuffer;
      } else if (event.data.action === 'clear') {
        this.buffer = new Float32Array(0);
      }
    };
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
        // Convert to PCM16 HERE, off the main thread
        const pcm16Buffer = this.floatTo16BitPCM(inputChannel);
        this.port.postMessage({
          action: 'record',
          audio: pcm16Buffer
        }, [pcm16Buffer]); // Transfer the buffer for zero-copy performance
      }
    }

    // 2. Handle Playback (Buffer -> Output)
    const output = outputs[0];
    if (output && output.length > 0) {
      const outputChannel = output[0];
      const length = outputChannel.length;

      if (this.buffer.length >= length) {
        outputChannel.set(this.buffer.slice(0, length));
        this.buffer = this.buffer.slice(length);
      } else {
        outputChannel.set(this.buffer);
        outputChannel.fill(0, this.buffer.length);
        this.buffer = new Float32Array(0);
      }
    }

    return true;
  }
}

registerProcessor('audio-processor', AudioProcessor);
