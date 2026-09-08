// Захват микрофона: копит блоки и отдаёт Float32 порциями ~40 мс.
// Ресемплинг в 16 кГц делается в основном потоке, здесь только буферизация.
class MicCapture extends AudioWorkletProcessor {
  constructor() {
    super();
    this.buffer = [];
    this.samples = 0;
    // sampleRate — реальная частота AudioContext (обычно 48000), приходит из глобали воркета
    this.chunkSize = Math.round(sampleRate * 0.04); // 40 мс
  }

  process(inputs) {
    const input = inputs[0];
    if (!input || !input[0]) return true;
    const channel = input[0];
    this.buffer.push(new Float32Array(channel));
    this.samples += channel.length;
    if (this.samples >= this.chunkSize) {
      const merged = new Float32Array(this.samples);
      let offset = 0;
      for (const part of this.buffer) {
        merged.set(part, offset);
        offset += part.length;
      }
      this.port.postMessage({ samples: merged, rate: sampleRate }, [merged.buffer]);
      this.buffer = [];
      this.samples = 0;
    }
    return true;
  }
}

registerProcessor('mic-capture', MicCapture);
