// ВЕКТОР Live — клиент: микрофон, показ экрана, воспроизведение ответа, карточки поручений.
const PARAMS = new URLSearchParams(location.search);
// вход бывает двух видов: локальный запуск (token) и одноразовая ссылка от ВЕКТОРА (k)
const LINK_KEY = PARAMS.get('k') || '';
const TOKEN = PARAMS.get('token') || '';
const AUTH_QUERY = LINK_KEY
  ? 'k=' + encodeURIComponent(LINK_KEY)
  : 'token=' + encodeURIComponent(TOKEN);
const IN_RATE = 16000;   // отправляем в Gemini
const OUT_RATE = 24000;  // приходит от Gemini
const DUCK = 0.12;       // насколько приглушаем микрофон, пока ВЕКТОР говорит (~ -18 дБ)

const el = (id) => document.getElementById(id);
const ui = {
  start: el('btn-start'), stop: el('btn-stop'), mic: el('btn-mic'),
  screen: el('btn-screen'), screenOff: el('btn-screen-off'), look: el('btn-look'),
  send: el('btn-send'), input: el('input-text'),
  attach: el('btn-attach'), file: el('file-image'),
  stateLine: el('state-line'), gemini: el('st-gemini'), hermesState: el('st-hermes'),
  micState: el('st-mic'), screenState: el('st-screen'), frameAge: el('st-frame'),
  usage: el('st-usage'), hands: el('st-hands'), mode: el('st-mode'), modeNote: el('mode-note'),
  modes: [...document.querySelectorAll('.mode')],
  chat: el('chat'), tasks: el('tasks'), preview: el('preview'), source: el('src-name'),
};

let ws = null;
let audioCtx = null;          // общий контекст (микрофон + воспроизведение)
let micStream = null, micNode = null, micSource = null, micGain = null;
let playerNode = null;
let screenStream = null, videoEl = null, canvasEl = null;
let lastFrameAt = 0;
let running = false;
let vektorSpeaking = false;
let mode = 'watch';
const MODE_NOTE = {
  watch: 'Видит экран и говорит. Ничего не трогает.',
  hint: 'Подсвечивает на экране, куда нажать. Нажимаете вы.',
  act: 'Нажимает и печатает сам. Необратимое всё равно оставит вам.',
};
const bubbles = { user: null, vektor: null };

// ---------- служебное ----------

function setState(text, cls) {
  ui.stateLine.textContent = text;
  ui.stateLine.className = 'state ' + (cls || '');
}

function line(role, text) {
  // склеиваем поток транскрипции в один пузырь, пока роль не сменилась
  let node = bubbles[role];
  if (!node) {
    node = document.createElement('div');
    node.className = 'msg ' + role;
    node.innerHTML = `<span class="who">${role === 'user' ? 'Вы' : 'ВЕКТОР'}</span><span class="t"></span>`;
    ui.chat.appendChild(node);
    bubbles[role] = node;
    bubbles[role === 'user' ? 'vektor' : 'user'] = null;
  }
  node.querySelector('.t').textContent += text;
  ui.chat.scrollTop = ui.chat.scrollHeight;
}

function systemLine(text) {
  const node = document.createElement('div');
  node.className = 'msg sys';
  node.textContent = text;
  ui.chat.appendChild(node);
  ui.chat.scrollTop = ui.chat.scrollHeight;
  bubbles.user = bubbles.vektor = null;
}

// ---------- картинки от Павла ----------
// Скриншот или фото можно вставить из буфера, перетащить в переписку или выбрать кнопкой.

const MAX_SIDE = 1280;

function imageBubble(dataUrl, caption) {
  const node = document.createElement('div');
  node.className = 'msg user';
  node.innerHTML = `<span class="who">Вы</span><span class="t"></span>`;
  node.querySelector('.t').textContent = caption || 'Картинка';
  const img = document.createElement('img');
  img.src = dataUrl;
  img.alt = 'отправленная картинка';
  node.appendChild(img);
  ui.chat.appendChild(node);
  ui.chat.scrollTop = ui.chat.scrollHeight;
  bubbles.user = bubbles.vektor = null;
}

async function sendImage(file) {
  if (!ws || ws.readyState !== WebSocket.OPEN) { systemLine('Сначала начните разговор.'); return; }
  if (!file || !file.type.startsWith('image/')) return;
  const bitmap = await createImageBitmap(file);
  const scale = Math.min(1, MAX_SIDE / Math.max(bitmap.width, bitmap.height));
  const canvas = document.createElement('canvas');
  canvas.width = Math.round(bitmap.width * scale);
  canvas.height = Math.round(bitmap.height * scale);
  canvas.getContext('2d').drawImage(bitmap, 0, 0, canvas.width, canvas.height);
  bitmap.close();
  const dataUrl = canvas.toDataURL('image/jpeg', 0.85);
  ws.send(JSON.stringify({ type: 'image', data: dataUrl.split(',')[1], name: file.name || '' }));
  imageBubble(dataUrl, file.name ? `Картинка: ${file.name}` : 'Картинка');
}

async function sendImages(files) {
  for (const f of [...files].slice(0, 4)) await sendImage(f);
}

// ---------- воспроизведение ----------
// Весь звук идёт через один воркет с кольцевым буфером: куски складываются в общий
// поток и ресемплируются сквозной интерполяцией. Отдельных источников на каждый кусок
// больше нет — не рвётся фаза на границах и не сбрасывается расписание при джиттере.

async function initPlayer() {
  await audioCtx.audioWorklet.addModule('/static/player-worklet.js');
  playerNode = new AudioWorkletNode(audioCtx, 'vektor-player', {
    numberOfInputs: 0, numberOfOutputs: 1, outputChannelCount: [1],
  });
  playerNode.connect(audioCtx.destination);
  playerNode.port.onmessage = (event) => {
    if (event.data.type === 'speaking') setSpeaking(event.data.value);
  };
}

function playChunk(buf) {
  if (!playerNode) return;
  playerNode.port.postMessage({ type: 'audio', pcm: buf }, [buf]);
}

function stopPlayback() {
  if (playerNode) playerNode.port.postMessage({ type: 'stop' });
}

function setSpeaking(value) {
  vektorSpeaking = value;
  // пока ВЕКТОР говорит, микрофон приглушаем: это убирает акустическую петлю
  // через динамики и заодно не даёт ему перебивать самого себя
  if (micGain && audioCtx) {
    const t = audioCtx.currentTime;
    micGain.gain.cancelScheduledValues(t);
    micGain.gain.setValueAtTime(micGain.gain.value, t);
    micGain.gain.linearRampToValueAtTime(value ? DUCK : 1, t + 0.08);
  }
  if (micStream) {
    ui.micState.textContent = value ? 'приглушён (ВЕКТОР говорит)' : 'включён';
    ui.micState.className = value ? 'warn' : 'ok';
  }
}

// ---------- микрофон ----------

function downsample(samples, fromRate) {
  if (fromRate === IN_RATE) return samples;
  const ratio = fromRate / IN_RATE;
  const out = new Float32Array(Math.floor(samples.length / ratio));
  for (let i = 0; i < out.length; i++) {
    const start = Math.floor(i * ratio);
    const end = Math.min(Math.floor((i + 1) * ratio), samples.length);
    let sum = 0;
    for (let j = start; j < end; j++) sum += samples[j];
    out[i] = sum / Math.max(1, end - start);
  }
  return out;
}

async function startMic() {
  micStream = await navigator.mediaDevices.getUserMedia({
    audio: { echoCancellation: true, noiseSuppression: true, autoGainControl: true, channelCount: 1 },
  });
  await audioCtx.audioWorklet.addModule('/static/mic-worklet.js');
  micSource = audioCtx.createMediaStreamSource(micStream);
  micGain = audioCtx.createGain();
  micGain.gain.value = vektorSpeaking ? DUCK : 1;
  micNode = new AudioWorkletNode(audioCtx, 'mic-capture');
  micNode.port.onmessage = (event) => {
    if (!ws || ws.readyState !== WebSocket.OPEN) return;
    const { samples, rate } = event.data;
    const down = downsample(samples, rate);
    const pcm = new Int16Array(down.length);
    for (let i = 0; i < down.length; i++) {
      const v = Math.max(-1, Math.min(1, down[i]));
      pcm[i] = v < 0 ? v * 32768 : v * 32767;
    }
    ws.send(pcm.buffer);
  };
  micSource.connect(micGain);
  micGain.connect(micNode);
  // воркет не соединяем с выходом: эхо не нужно
  ui.micState.textContent = 'включён';
  ui.micState.className = 'ok';
  ui.mic.textContent = 'Выключить микрофон';
}

function stopMic() {
  if (micNode) { micNode.port.onmessage = null; micNode.disconnect(); micNode = null; }
  if (micGain) { micGain.disconnect(); micGain = null; }
  if (micSource) { micSource.disconnect(); micSource = null; }
  if (micStream) { micStream.getTracks().forEach((t) => t.stop()); micStream = null; }
  ui.micState.textContent = 'выключен';
  ui.micState.className = 'off';
  ui.mic.textContent = 'Включить микрофон';
}

// ---------- экран ----------
// Кадры больше не идут потоком. Экран снимается только когда ВЕКТОР сам решил
// посмотреть (инструмент look_at_screen) или когда Павел нажал «Посмотри на экран».

async function startScreen() {
  screenStream = await navigator.mediaDevices.getDisplayMedia({
    video: { frameRate: 2 }, audio: false,
  });
  const track = screenStream.getVideoTracks()[0];
  ui.source.textContent = track.label || 'выбранный источник';
  track.addEventListener('ended', () => stopScreen(true));

  videoEl = document.createElement('video');
  videoEl.srcObject = screenStream;
  videoEl.muted = true;
  await videoEl.play();
  canvasEl = document.createElement('canvas');

  ws.send(JSON.stringify({ type: 'screen_on', source: track.label || '' }));
  ui.screenState.textContent = 'доступен';
  ui.screenState.className = 'ok';
  ui.screen.disabled = true;
  ui.screenOff.disabled = false;
  ui.look.disabled = false;
  systemLine('Экран доступен. ВЕКТОР снимет кадр, когда ему понадобится, — или нажмите «Посмотри на экран».');
  sendFrame();
}

function sendFrame() {
  if (!videoEl || !ws || ws.readyState !== WebSocket.OPEN) return false;
  const w = videoEl.videoWidth, h = videoEl.videoHeight;
  if (!w || !h) return false;
  const scale = Math.min(1, 1280 / Math.max(w, h));
  canvasEl.width = Math.round(w * scale);
  canvasEl.height = Math.round(h * scale);
  canvasEl.getContext('2d').drawImage(videoEl, 0, 0, canvasEl.width, canvasEl.height);
  const dataUrl = canvasEl.toDataURL('image/jpeg', 0.75);
  ws.send(JSON.stringify({ type: 'frame', data: dataUrl.split(',')[1],
                           width: canvasEl.width, height: canvasEl.height }));
  lastFrameAt = Date.now();
  ui.preview.src = dataUrl;
  ui.preview.style.display = 'block';
  return true;
}

function stopScreen(fromTrack) {
  if (screenStream) { screenStream.getTracks().forEach((t) => t.stop()); screenStream = null; }
  videoEl = null; canvasEl = null; lastFrameAt = 0;
  ui.preview.style.display = 'none';
  ui.source.textContent = '—';
  ui.screenState.textContent = 'выключен';
  ui.screenState.className = 'off';
  ui.screen.disabled = !running;
  ui.screenOff.disabled = true;
  ui.look.disabled = true;
  if (ws && ws.readyState === WebSocket.OPEN) ws.send(JSON.stringify({ type: 'screen_off' }));
  if (fromTrack) systemLine('Показ экрана остановлен системой.');
}

setInterval(() => {
  ui.frameAge.textContent = lastFrameAt ? Math.round((Date.now() - lastFrameAt) / 1000) + ' с назад' : '—';
}, 500);

// ---------- поручения ----------

function renderTask(card) {
  let node = document.getElementById('task-' + card.task_id);
  if (!node) {
    node = document.createElement('div');
    node.id = 'task-' + card.task_id;
    node.className = 'task';
    ui.tasks.prepend(node);
  }
  const labels = {
    awaiting_confirmation: 'ждёт вашего подтверждения',
    running: 'Гермес выполняет…', done: 'готово', error: 'ошибка', cancelled: 'отменено',
  };
  const body = [];
  body.push(`<div class="task-head"><b>Поручение Гермесу</b><span class="badge ${card.status}">${labels[card.status] || card.status}</span></div>`);
  body.push(`<div class="task-payload">${escapeHtml(card.payload)}</div>`);
  if (card.status === 'awaiting_confirmation') {
    body.push(`<div class="task-actions">
      <button data-confirm="${card.task_id}">Передать Гермесу</button>
      <button class="ghost" data-cancel="${card.task_id}">Отменить</button></div>`);
  }
  if (card.result) body.push(`<div class="task-result">${escapeHtml(card.result)}</div>`);
  if (card.error) body.push(`<div class="task-error">${escapeHtml(card.error)}</div>`);
  node.innerHTML = body.join('');
}

function escapeHtml(s) {
  return (s || '').replace(/[&<>"]/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));
}

ui.tasks.addEventListener('click', (e) => {
  const confirmId = e.target.getAttribute('data-confirm');
  const cancelId = e.target.getAttribute('data-cancel');
  if (confirmId) ws.send(JSON.stringify({ type: 'confirm', task_id: confirmId }));
  if (cancelId) ws.send(JSON.stringify({ type: 'cancel', task_id: cancelId }));
});

// ---------- соединение ----------

async function start() {
  if (running) return;
  audioCtx = new (window.AudioContext || window.webkitAudioContext)();
  await audioCtx.resume(); // разблокировка звука жестом пользователя
  await initPlayer();

  // на HTTPS браузер запрещает незащищённый ws:// — протокол выбираем по странице
  const wsScheme = location.protocol === 'https:' ? 'wss' : 'ws';
  ws = new WebSocket(`${wsScheme}://${location.host}/ws?${AUTH_QUERY}`);
  ws.binaryType = 'arraybuffer';

  ws.onopen = async () => {
    running = true;
    setState('Разговор идёт. Говорите — ВЕКТОР слушает.', 'live');
    ui.start.disabled = true; ui.stop.disabled = false;
    ui.mic.disabled = false; ui.screen.disabled = false; ui.send.disabled = false;
    ui.modes.forEach((b) => { b.disabled = b.dataset.mode !== 'watch'; });
    ui.attach.disabled = false;
    try { await startMic(); } catch (err) {
      systemLine('Микрофон не разрешён: ' + err.message + '. Разрешите доступ и нажмите «Включить микрофон».');
    }
  };

  ws.onmessage = (event) => {
    if (event.data instanceof ArrayBuffer) { playChunk(event.data); return; }
    const msg = JSON.parse(event.data);
    if (msg.type === 'transcript') line(msg.role, msg.text);
    else if (msg.type === 'interrupted') stopPlayback();
    else if (msg.type === 'turn_complete') { bubbles.user = bubbles.vektor = null; }
    else if (msg.type === 'task') renderTask(msg.card);
    else if (msg.type === 'request_frame') {
      // ВЕКТОР сам попросил кадр — отдаём текущий снимок экрана
      const sent = sendFrame();
      if (!sent) ws.send(JSON.stringify({ type: 'frame_unavailable' }));
    }
    else if (msg.type === 'shot') {
      // кадр снял агент на маке — показываем тот же снимок, что ушёл модели
      ui.preview.src = 'data:image/jpeg;base64,' + msg.image;
      ui.preview.style.display = 'block';
      lastFrameAt = Date.now();
    }
    else if (msg.type === 'usage') {
      ui.usage.textContent = msg.text;
      ui.usage.className = '';
      ui.usage.title = msg.detail || '';
    }
    else if (msg.type === 'status') {
      if (msg.gemini) { ui.gemini.textContent = msg.gemini; ui.gemini.className = msg.gemini === 'подключено' ? 'ok' : 'warn'; }
      if (msg.hands) {
        ui.hands.textContent = msg.hands;
        ui.hands.className = msg.hands === 'подключены' ? 'ok' : 'off';
        ui.modes.forEach((b) => { if (b.dataset.mode !== 'watch') b.disabled = !running || msg.hands !== 'подключены'; });
        if (msg.hands !== 'подключены' && mode !== 'watch') setMode('watch');
      }
      if (msg.mode_name) ui.mode.textContent = msg.mode_name;
      if (msg.hermes) { ui.hermesState.textContent = msg.hermes; ui.hermesState.className = msg.hermes === 'доступен' ? 'ok' : 'off'; ui.hermesState.title = msg.hermes_detail || ''; }
      if (msg.detail) systemLine('Gemini: ' + msg.detail);
    }
  };

  ws.onclose = () => { if (running) { setState('Соединение закрыто.', 'off'); finish(); } };
  ws.onerror = () => setState('Ошибка соединения с сервисом.', 'off');
}

function finish() {
  running = false;
  stopMic();
  if (screenStream) stopScreen(false);
  stopPlayback();
  if (playerNode) { playerNode.port.onmessage = null; playerNode.disconnect(); playerNode = null; }
  if (ws && ws.readyState === WebSocket.OPEN) ws.close();
  if (audioCtx) { audioCtx.close(); audioCtx = null; }
  ui.start.disabled = false; ui.stop.disabled = true;
  ui.mic.disabled = true; ui.screen.disabled = true; ui.screenOff.disabled = true;
  ui.look.disabled = true; ui.send.disabled = true;
  ui.modes.forEach((b) => { b.disabled = true; });
  ui.attach.disabled = true;
  setMode('watch');
  ui.hands.textContent = 'не подключены'; ui.hands.className = 'off';
  ui.gemini.textContent = 'отключено'; ui.gemini.className = 'off';
  setState('Разговор завершён.', 'off');
}

function setMode(next) {
  mode = next;
  ui.modes.forEach((b) => {
    b.classList.toggle('is-on', b.dataset.mode === next);
    b.classList.toggle('is-act', b.dataset.mode === 'act');
  });
  ui.modeNote.textContent = MODE_NOTE[next];
  if (ws && ws.readyState === WebSocket.OPEN) ws.send(JSON.stringify({ type: 'mode', mode: next }));
}
ui.modes.forEach((b) => { b.onclick = () => setMode(b.dataset.mode); });

ui.start.onclick = start;
ui.stop.onclick = finish;
ui.mic.onclick = async () => { if (micStream) stopMic(); else { try { await startMic(); } catch (e) { systemLine('Микрофон недоступен: ' + e.message); } } };
ui.screen.onclick = async () => { try { await startScreen(); } catch (e) { systemLine('Показ экрана не начат: ' + e.message); } };
ui.screenOff.onclick = () => stopScreen(false);
ui.look.onclick = () => {
  if (!sendFrame()) { systemLine('Кадр не получен — показ экрана не идёт.'); return; }
  ws.send(JSON.stringify({ type: 'text', text: 'Посмотри на экран и скажи, что видишь.' }));
};
ui.send.onclick = () => {
  const text = ui.input.value.trim();
  if (!text || !ws || ws.readyState !== WebSocket.OPEN) return;
  ws.send(JSON.stringify({ type: 'text', text }));
  ui.input.value = '';
};
ui.input.addEventListener('keydown', (e) => { if (e.key === 'Enter') ui.send.click(); });

ui.attach.onclick = () => ui.file.click();
ui.file.onchange = () => { sendImages(ui.file.files); ui.file.value = ''; };
document.addEventListener('paste', (e) => {
  const files = [...(e.clipboardData?.files || [])].filter((f) => f.type.startsWith('image/'));
  if (files.length) { e.preventDefault(); sendImages(files); }
});
['dragenter', 'dragover'].forEach((ev) => ui.chat.addEventListener(ev, (e) => {
  e.preventDefault(); ui.chat.classList.add('drop');
}));
['dragleave', 'drop'].forEach((ev) => ui.chat.addEventListener(ev, (e) => {
  e.preventDefault(); ui.chat.classList.remove('drop');
}));
ui.chat.addEventListener('drop', (e) => { if (e.dataTransfer?.files?.length) sendImages(e.dataTransfer.files); });
window.addEventListener('beforeunload', finish);
