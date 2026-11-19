/* Bounded continuous resampling. Captured samples are never stored persistently. */
class PCMResampler {
  constructor(inputRate, outputRate = 16000, packetSize = 1600, emit = () => {}) {
    this.ratio = inputRate / outputRate;
    this.packetSize = packetSize; this.emit = emit;
    this.packet = new Float32Array(packetSize);
    this.used = 0; this.index = 0; this.next = 0; this.previous = 0;
    this.taps = inputRate > outputRate ? 63 : 1;
    this.history = new Float32Array(this.taps); this.cursor = 0;
    this.kernel = new Float64Array(this.taps);
    if (this.taps === 1) this.kernel[0] = 1;
    else {
      const cutoff = 7000 / inputRate, center = (this.taps - 1) / 2;
      let sum = 0;
      for (let i = 0; i < this.taps; i++) {
        const x = i - center;
        const sinc = x === 0 ? 2 * cutoff : Math.sin(2 * Math.PI * cutoff * x) / (Math.PI * x);
        const window = .54 - .46 * Math.cos(2 * Math.PI * i / (this.taps - 1));
        this.kernel[i] = sinc * window; sum += this.kernel[i];
      }
      for (let i = 0; i < this.taps; i++) this.kernel[i] /= sum;
    }
  }
  feed(input) {
    for (let i = 0; i < input.length; i++) {
      this.history[this.cursor] = input[i];
      let current = 0, slot = this.cursor;
      for (let j = 0; j < this.taps; j++) {
        current += this.history[slot] * this.kernel[j];
        slot = slot === 0 ? this.taps - 1 : slot - 1;
      }
      this.cursor = (this.cursor + 1) % this.taps;
      if (this.index === 0) this.previous = current;
      while (this.next <= this.index) {
        const fraction = Math.max(0, this.next - (this.index - 1));
        const value = this.previous + (current - this.previous) * fraction;
        this.packet[this.used++] = Number.isFinite(value) ? Math.max(-1, Math.min(1, value)) : 0;
        if (this.used === this.packetSize) this.flush();
        this.next += this.ratio;
      }
      this.previous = current; this.index++;
    }
  }
  flush() {
    if (!this.used) return;
    const packet = this.packet.slice(0, this.used);
    this.used = 0; this.emit(packet);
  }
}
if (typeof AudioWorkletProcessor !== 'undefined') {
  class MotetDictationPCM extends AudioWorkletProcessor {
    constructor() {
      super(); this.finished = false;
      this.resampler = new PCMResampler(sampleRate, 16000, 1600, packet => {
        this.port.postMessage({ type: 'pcm', buffer: packet.buffer }, [packet.buffer]);
      });
      this.port.onmessage = ({ data }) => {
        if (data.type === 'flush') {
          this.resampler.flush(); this.finished = true;
          this.port.postMessage({ type: 'flushed', requestId: data.requestId });
        }
      };
    }
    process(inputs, outputs) {
      for (const output of outputs) for (const channel of output) channel.fill(0);
      if (this.finished) return false;
      const channels = inputs[0];
      if (channels && channels[0]) {
        if (channels.length === 1) this.resampler.feed(channels[0]);
        else {
          const mono = new Float32Array(channels[0].length);
          for (const channel of channels) for (let i = 0; i < mono.length; i++) mono[i] += channel[i] / channels.length;
          this.resampler.feed(mono);
        }
      }
      return true;
    }
  }
  registerProcessor('motet-dictation-pcm', MotetDictationPCM);
}
if (typeof module !== 'undefined' && module.exports) module.exports = { PCMResampler };
