// Воспроизведение ответа ВЕКТОРА одним непрерывным потоком.
//
// Зачем воркет. Раньше каждый кусок PCM игрался отдельным AudioBufferSourceNode
// со своей частотой 24 кГц. Браузер ресемплил каждый кусок независимо от соседей,
// поэтому на каждой границе рвалась фаза, а при сетевом джиттере очередь пустела
// и расписание сбрасывалось. Частый регулярный треск ухо слышит как писк.
// Здесь куски складываются в кольцевой буфер, а ресемплинг идёт сквозной:
// дробная позиция чтения переходит через границы кусков и фаза не рвётся.

const SRC_RATE = 24000;                 // частота, в которой отдаёт Gemini
const RING_SECONDS = 12;
const FADE_MS = 12;                     // огибающая на старте, паузе и перебивании

class VektorPlayer extends AudioWorkletProcessor {
  constructor() {
    super();
    this.ring = new Float32Array(SRC_RATE * RING_SECONDS);
    this.wpos = 0;              // сколько сэмплов источника записано всего
    this.rpos = 0;              // дробная позиция чтения в тех же единицах
    this.step = SRC_RATE / sampleRate;
    this.startThreshold = SRC_RATE * 0.18;  // старт фразы — после 180 мс запаса
    this.resumeThreshold = SRC_RATE * 0.06; // добор после провала — 60 мс
    this.active = false;
    this.gain = 0;
    this.gainStep = 1 / Math.max(1, sampleRate * FADE_MS / 1000);
    this.stopping = false;
    this.speaking = false;

    this.port.onmessage = (event) => {
      const msg = event.data;
      if (msg.type === 'audio') this.push(new Int16Array(msg.pcm));
      else if (msg.type === 'stop') this.stopping = true;
    };
  }

  push(pcm) {
    const size = this.ring.length;
    // защита от переполнения: если клиент отстал больше чем на буфер, двигаем чтение
    if (this.wpos - this.rpos + pcm.length > size) {
      this.rpos = this.wpos + pcm.length - size + 1;
    }
    for (let i = 0; i < pcm.length; i++) {
      this.ring[(this.wpos + i) % size] = pcm[i] / 32768;
    }
    this.wpos += pcm.length;
  }

  process(_inputs, outputs) {
    const out = outputs[0][0];
    if (!out) return true;
    const size = this.ring.length;

    for (let i = 0; i < out.length; i++) {
      const avail = this.wpos - this.rpos;

      if (this.stopping) {
        this.gain = Math.max(0, this.gain - this.gainStep);
        if (this.gain === 0) {
          // погасили — выбрасываем остаток запланированной речи
          this.rpos = this.wpos;
          this.active = false;
          this.stopping = false;
        }
      } else if (!this.active) {
        // ждём, пока накопится запас: старт фразы или добор после провала
        const need = this.gain > 0 ? this.resumeThreshold : this.startThreshold;
        if (avail >= need) this.active = true;
        this.gain = Math.max(0, this.gain - this.gainStep);
      } else if (avail <= 1) {
        // очередь опустела посреди фразы — уходим в тишину, но фазу не сбрасываем
        this.active = false;
        this.gain = Math.max(0, this.gain - this.gainStep);
      } else {
        this.gain = Math.min(1, this.gain + this.gainStep);
      }

      let sample = 0;
      if (this.active && this.wpos - this.rpos > 1) {
        const idx = Math.floor(this.rpos);
        const frac = this.rpos - idx;
        const a = this.ring[idx % size];
        const b = this.ring[(idx + 1) % size];
        sample = a + (b - a) * frac;   // сквозная интерполяция, границ кусков не существует
        this.rpos += this.step;
      }
      out[i] = sample * this.gain;
    }

    // сообщаем основному потоку, говорит ВЕКТОР или молчит — по этому приглушается микрофон
    const speaking = this.gain > 0.001 || this.active;
    if (speaking !== this.speaking) {
      this.speaking = speaking;
      this.port.postMessage({ type: 'speaking', value: speaking });
    }
    return true;
  }
}

registerProcessor('vektor-player', VektorPlayer);
