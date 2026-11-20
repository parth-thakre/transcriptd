// Microphone -> int16 LE mono 16 kHz frames for /v1/dictate, plus a level for the UI.
// The AudioContext normally runs at 16 kHz already; otherwise this box-filters down.
const FRAME = 640; // 40 ms at 16 kHz

class PcmCapture extends AudioWorkletProcessor {
  constructor() {
    super();
    this.ratio = sampleRate / 16000;
    this.acc = 0;
    this.accN = 0;
    this.pos = 0;
    this.frame = new Int16Array(FRAME);
    this.fill = 0;
    this.peak = 0;
    this.port.onmessage = event => {
      if (event.data !== 'flush' || !this.fill) return;
      const tail = this.frame.slice(0, this.fill);
      this.port.postMessage({ pcm: tail, level: this.peak }, [tail.buffer]);
      this.fill = 0;
    };
  }

  process(inputs) {
    const input = inputs[0] && inputs[0][0];
    if (!input) return true;
    for (let i = 0; i < input.length; i++) {
      this.acc += input[i];
      this.accN++;
      this.pos += 1;
      if (this.pos < this.ratio) continue;
      this.pos -= this.ratio;
      const s = Math.max(-1, Math.min(1, this.acc / this.accN));
      this.acc = 0;
      this.accN = 0;
      this.peak = Math.max(this.peak, Math.abs(s));
      this.frame[this.fill++] = s < 0 ? s * 0x8000 : s * 0x7fff;
      if (this.fill === FRAME) {
        this.port.postMessage({ pcm: this.frame, level: this.peak }, [this.frame.buffer]);
        this.frame = new Int16Array(FRAME);
        this.fill = 0;
        this.peak = 0;
      }
    }
    return true;
  }
}

registerProcessor('pcm-capture', PcmCapture);
