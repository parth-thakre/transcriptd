// transcriptd phone app: hold (or tap) to dictate, get guarded clean text, already copied.
// Talks to the same /v1 API as the desktop agent. No build step; plain ES modules.
import { annotate, describe, tone } from './annotate.js';

const $ = id => document.getElementById(id);
const STORE = 'transcriptd.m';
const HOLD_MS = 350; // released sooner than this = tap = hands-free
const CANCEL_PX = 90; // drag this far off the button while holding to discard
const MAX_SECONDS = 590; // server stops at 600
const MIC_IDLE_MS = 20_000; // keep the mic open this long after a dictation so the next starts instantly

// -- settings -----------------------------------------------------------------------------

const prefs = Object.assign({ token: '', profile: '', style: 'auto', autoCopy: true, haptics: true, llm: true },
  JSON.parse(localStorage.getItem(STORE) || '{}'));
const save = () => localStorage.setItem(STORE, JSON.stringify(prefs));

let health = null;

// -- icons --------------------------------------------------------------------------------

const svg = d => `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${d}</svg>`;
const ICONS = {
  mic: svg('<rect x="9" y="3" width="6" height="11" rx="3"/><path d="M5 11a7 7 0 0 0 14 0M12 18v3"/>'),
  stop: '<svg viewBox="0 0 24 24" aria-hidden="true"><rect x="6" y="6" width="12" height="12" rx="2.5" fill="currentColor"/></svg>',
  close: svg('<path d="M6 6l12 12M18 6L6 18"/>'),
  history: svg('<path d="M3 12a9 9 0 1 0 3-6.7L3 8"/><path d="M3 3v5h5M12 7v5l3 2"/>'),
  back: svg('<path d="M15 5l-7 7 7 7"/>'),
  trash: svg('<path d="M4 7h16M10 11v6M14 11v6M6 7l1 13h10l1-13M9 7V4h6v3"/>'),
  share: svg('<circle cx="18" cy="5" r="2.5"/><circle cx="6" cy="12" r="2.5"/><circle cx="18" cy="19" r="2.5"/><path d="M8.2 10.8l7.6-4.4M8.2 13.2l7.6 4.4"/>'),
  copy: svg('<rect x="9" y="9" width="11" height="11" rx="2"/><path d="M5 15V5a2 2 0 0 1 2-2h8"/>'),
  check: svg('<path d="M5 12.5l4.5 4.5L19 7"/>'),
  gear: svg('<circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.7 1.7 0 0 0 .3 1.8l.1.1a2 2 0 1 1-2.8 2.8l-.1-.1a1.7 1.7 0 0 0-1.8-.3 1.7 1.7 0 0 0-1 1.5V21a2 2 0 1 1-4 0v-.1a1.7 1.7 0 0 0-1.1-1.5 1.7 1.7 0 0 0-1.8.3l-.1.1a2 2 0 1 1-2.8-2.8l.1-.1a1.7 1.7 0 0 0 .3-1.8 1.7 1.7 0 0 0-1.5-1H3a2 2 0 1 1 0-4h.1a1.7 1.7 0 0 0 1.5-1.1 1.7 1.7 0 0 0-.3-1.8l-.1-.1a2 2 0 1 1 2.8-2.8l.1.1a1.7 1.7 0 0 0 1.8.3H9a1.7 1.7 0 0 0 1-1.5V3a2 2 0 1 1 4 0v.1a1.7 1.7 0 0 0 1 1.5 1.7 1.7 0 0 0 1.8-.3l.1-.1a2 2 0 1 1 2.8 2.8l-.1.1a1.7 1.7 0 0 0-.3 1.8V9a1.7 1.7 0 0 0 1.5 1H21a2 2 0 1 1 0 4h-.1a1.7 1.7 0 0 0-1.5 1z"/>'),
  retry: svg('<path d="M20 11a8 8 0 1 0-2.3 5.7"/><path d="M20 4v7h-7"/>'),
  chevron: svg('<path d="M7 10l5 5 5-5"/>'),
};
function paintIcons(root = document) {
  for (const el of root.querySelectorAll('[data-icon]')) el.innerHTML = ICONS[el.dataset.icon] || '';
}

// -- small helpers ------------------------------------------------------------------------

const buzz = pattern => { if (prefs.haptics && navigator.vibrate) navigator.vibrate(pattern); };
const esc = s => s.replace(/[&<>"]/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' })[c]);

let toastTimer = 0;
function toast(message, action) {
  const el = $('toast');
  el.innerHTML = `<span>${esc(message)}</span>`;
  if (action) {
    const b = document.createElement('button');
    b.textContent = action.label;
    b.onclick = () => { el.hidden = true; action.run(); };
    el.append(b);
  }
  el.hidden = false;
  el.style.animation = 'none'; void el.offsetWidth; el.style.animation = '';
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { el.hidden = true; }, action ? 6000 : 2200);
}

async function api(path, opts = {}) {
  const headers = { Authorization: `Bearer ${prefs.token}`, ...(opts.json ? { 'Content-Type': 'application/json' } : {}), ...opts.headers };
  const r = await fetch(`/v1${path}`, {
    method: opts.method || (opts.json || opts.body ? 'POST' : 'GET'), headers,
    body: opts.json ? JSON.stringify(opts.json) : opts.body, signal: opts.signal ?? AbortSignal.timeout(opts.timeout ?? 15000),
  });
  if (r.status === 401) { unpair('This phone is no longer paired'); throw new Error('unauthorized'); }
  if (!r.ok) throw new Error(`${r.status} ${await r.text().catch(() => '')}`.trim());
  return r.status === 204 ? null : r.json();
}

async function copyText(text, quiet = false) {
  try {
    await navigator.clipboard.writeText(text);
  } catch {
    const ta = Object.assign(document.createElement('textarea'), { value: text });
    ta.style.cssText = 'position:fixed;opacity:0';
    document.body.append(ta); ta.select();
    const ok = document.execCommand('copy');
    ta.remove();
    if (!ok) { if (!quiet) toast('Copy failed. Long-press the text.'); return false; }
  }
  buzz([6, 40, 6]);
  return true;
}

function ago(iso) {
  const t = new Date(/Z|[+-]\d\d:\d\d$/.test(iso) ? iso : iso + 'Z').getTime();
  const s = Math.round((Date.now() - t) / 1000);
  if (s < 60) return 'just now';
  if (s < 3600) return `${Math.floor(s / 60)} min ago`;
  if (s < 86400) return `${Math.floor(s / 3600)} h ago`;
  return new Date(t).toLocaleDateString(undefined, { day: 'numeric', month: 'short' });
}
const secs = ms => ms >= 60000 ? `${Math.floor(ms / 60000)}:${String(Math.round(ms / 1000) % 60).padStart(2, '0')}` : `${(ms / 1000).toFixed(ms < 10000 ? 1 : 0)} s`;

// -- views and the back gesture -------------------------------------------------------------
// Home is the base; every other view is pushed so the Android back gesture walks back out.

const VIEWS = ['home', 'result', 'history', 'settings', 'pair'];
let view = 'home';

function render(name) {
  view = name;
  for (const v of VIEWS) $(v).hidden = v !== name;
  if (name === 'history') loadHistory();
  if (name === 'settings') renderSettings();
  if (name === 'result') requestAnimationFrame(fitText);
}

function go(name) {
  if (name === view) return;
  const depth = history.state?.depth || 0;
  if (name === 'home') {
    render('home');
    if (depth) history.go(-depth);
    return;
  }
  history.pushState({ view: name, depth: depth + 1 }, '');
  render(name);
}

addEventListener('popstate', () => {
  closeSheet();
  render(prefs.token ? (history.state?.view || 'home') : 'pair');
});
for (const b of document.querySelectorAll('[data-back]')) {
  b.onclick = () => { if (history.state?.depth) history.back(); else render('home'); };
}

// -- server status --------------------------------------------------------------------------

async function refreshHealth() {
  const dot = $('statusDot');
  try {
    health = await api('/health', { timeout: 4000 });
    dot.className = 'dot ok';
    const stt = sttProfiles();
    if (!stt.includes(prefs.profile)) prefs.profile = stt[0] || '';
    if (!health.styles.some(s => s.name === prefs.style)) prefs.style = health.styles[0]?.name || 'auto';
    save();
  } catch (e) {
    if (e.message === 'unauthorized') return;
    dot.className = 'dot bad';
  }
  $('profileName').textContent = prefs.profile || 'offline';
  renderStyleBar();
}

// Speech-to-text profiles only (never chat_audio): Whisper first, and of those a fixed-English
// one first, the most predictable for dictation.
function sttProfiles() {
  const rank = p => (/whisper/i.test(p.model || '') ? 0 : 2) + (p.language === 'en' ? 0 : 1);
  return Object.entries(health?.profiles || {}).filter(([, p]) => p.engine !== 'chat_audio')
    .sort(([, a], [, b]) => rank(a) - rank(b)).map(([name]) => name);
}

function styles() {
  return health?.styles?.length ? health.styles : [{ name: 'auto', label: 'Auto' }];
}
const styleLabel = name => name === 'verbatim' ? 'Verbatim' : styles().find(s => s.name === name)?.label || name;

function renderStyleBar() {
  $('styleChipLabel').textContent = styleLabel(prefs.style);
  const bar = $('styleBar');
  bar.innerHTML = '';
  for (const s of styles()) {
    const b = document.createElement('button');
    b.textContent = s.label;
    b.setAttribute('role', 'radio');
    b.setAttribute('aria-checked', String(s.name === prefs.style));
    b.onclick = () => { prefs.style = s.name; save(); buzz(4); renderStyleBar(); };
    bar.append(b);
  }
}

$('profileBtn').onclick = () => {
  if (!health) { refreshHealth(); return; }
  openOptions('Speech model', sttProfiles().map(name => ({
    label: name, note: (health.profiles[name].languages || []).join(' · '), checked: name === prefs.profile,
    run: () => { prefs.profile = name; save(); $('profileName').textContent = name; },
  })));
};
$('styleChip').onclick = () => openOptions('Clean up as', styles().map(s => ({
  label: s.label, checked: s.name === prefs.style,
  run: () => { prefs.style = s.name; save(); renderStyleBar(); },
})));
$('historyBtn').onclick = () => go('history');
$('settingsBtn').onclick = () => go('settings');

// -- microphone -----------------------------------------------------------------------------

const mic = {
  stream: null, ctx: null, source: null, node: null, idle: 0,

  async ready() {
    clearTimeout(this.idle);
    if (!this.stream || this.stream.getAudioTracks().every(t => t.readyState === 'ended')) {
      this.stream = await navigator.mediaDevices.getUserMedia({
        audio: { channelCount: 1, echoCancellation: true, noiseSuppression: true, autoGainControl: true },
      });
      this.source = null;
    }
    if (!this.ctx || this.ctx.state === 'closed') {
      try { this.ctx = new AudioContext({ sampleRate: 16000, latencyHint: 'interactive' }); }
      catch { this.ctx = new AudioContext({ latencyHint: 'interactive' }); }
      await this.ctx.audioWorklet.addModule('pcm-worklet.js');
      this.source = null;
    }
    if (this.ctx.state !== 'running') await this.ctx.resume();
    this.source ??= this.ctx.createMediaStreamSource(this.stream);
  },

  start(onFrame) {
    this.node = new AudioWorkletNode(this.ctx, 'pcm-capture');
    this.node.port.onmessage = e => onFrame(e.data.pcm, e.data.level);
    this.source.connect(this.node);
    this.node.connect(this.ctx.destination); // outputs silence; keeps the node pulled
  },

  /** Stop capturing; resolves after the last partial frame has been delivered. */
  stop() {
    const node = this.node;
    this.node = null;
    this.idle = setTimeout(() => this.release(), MIC_IDLE_MS);
    if (!node) return Promise.resolve();
    return new Promise(resolve => {
      node.port.postMessage('flush');
      setTimeout(() => { try { this.source?.disconnect(node); node.disconnect(); } catch {} resolve(); }, 60);
    });
  },

  release() {
    clearTimeout(this.idle);
    this.stream?.getTracks().forEach(t => t.stop());
    this.stream = null;
    this.source = null;
    this.ctx?.suspend().catch(() => {});
  },
};

document.addEventListener('visibilitychange', () => {
  if (document.hidden) {
    if (rec && (rec.state === 'recording' || rec.state === 'locked')) finishRecording();
    else if (!rec) mic.release();
  } else {
    refreshHealth();
  }
});

// -- one dictation --------------------------------------------------------------------------
// The socket and the mic open in parallel on press; audio is buffered until the server is
// ready, so nothing said in the first few hundred milliseconds is lost. Every frame is also
// kept locally: if the socket fails, the same audio is uploaded instead.

let rec = null;
let wakeLock = null;

function setRec(state) {
  if (rec) rec.state = state;
  $('home').dataset.rec = state;
  const glyph = $('mic').querySelector('.glyph');
  glyph.innerHTML = ICONS[state === 'locked' ? 'stop' : 'mic'];
  $('mic').setAttribute('aria-label', state === 'locked' ? 'Stop and clean up' : 'Hold to talk, or tap for hands-free');
  $('hint').textContent = {
    idle: 'Hold to talk · tap for hands-free',
    starting: 'Listening…',
    recording: 'Release to finish · slide away to cancel',
    locked: 'Tap to finish',
    cancel: 'Release to discard',
    processing: 'Cleaning up…',
  }[state] ?? '';
  if (state === 'idle' || state === 'processing') { $('timer').textContent = ''; $('halo').style.setProperty('--lvl', 0); }
}

/** A WebSocket can't carry the Authorization header, so each socket gets a single-use ticket
 *  (60 s) instead of the token in its URL, where access logs would keep it. */
async function wsUrl() {
  const { ticket } = await api('/ws-ticket', { method: 'POST', timeout: 5000 });
  const u = new URL('/v1/dictate', location.href);
  u.protocol = u.protocol === 'https:' ? 'wss:' : 'ws:';
  u.searchParams.set('ticket', ticket);
  return u.href;
}

async function openSocket(r) {
  r.final = new Promise((resolve, reject) => { r.resolveFinal = resolve; r.rejectFinal = reject; });
  r.final.catch(() => {});
  r.isReady = new Promise(resolve => { r.resolveReady = resolve; });
  let ws;
  try {
    const url = await wsUrl();
    // Cancelled, or the mic failed to open, while the ticket came: don't open a socket to idle.
    if (r.failed || (r.ended && rec !== r)) throw new Error('discarded');
    ws = new WebSocket(url);
  } catch (e) { r.failed = true; r.rejectFinal(e); r.resolveReady(); return; } // finish falls back to the upload
  ws.binaryType = 'arraybuffer';
  r.ws = ws;
  ws.onopen = () => ws.send(JSON.stringify({
    type: 'start', profile: prefs.profile || null, style: prefs.style === 'verbatim' ? 'auto' : prefs.style,
    partials: true, app_hint: 'phone', llm: prefs.llm,
  }));
  ws.onmessage = e => {
    const m = JSON.parse(e.data);
    if (m.type === 'ready') {
      for (const f of r.frames) ws.send(f);
      r.ready = true;
      r.resolveReady();
      if (r.ended) ws.send(JSON.stringify({ type: 'end' }));
    } else if (m.type === 'partial') {
      if (r === rec && r.state !== 'processing') showLive(m.text);
    } else if (m.type === 'final') {
      r.resolveFinal(m);
      ws.close();
    } else if (m.type === 'error') {
      r.rejectFinal(new Error(m.message));
      ws.close();
    }
  };
  ws.onerror = () => { r.failed = true; r.rejectFinal(new Error('socket error')); };
  ws.onclose = () => { r.failed = true; r.rejectFinal(new Error('socket closed')); };
}

function showLive(text) {
  const live = $('live');
  live.innerHTML = text ? `<span>${esc(text)}</span>` : '';
}

let micGranted = false;

async function startRecording() {
  // `rec` is set before any await so a release during start-up is never lost.
  const r = rec = { state: 'starting', frames: [], samples: 0, ready: false, ended: false, failed: false, profile: prefs.profile, style: prefs.style };
  setRec('starting');
  showLive('');
  if (!micGranted) {
    const perm = await navigator.permissions?.query({ name: 'microphone' }).catch(() => null);
    micGranted = perm?.state === 'granted';
    if (!micGranted) {
      // The permission prompt would swallow the hold. Ask first; the next press records.
      rec = null;
      press = null;
      setRec('idle');
      try { await mic.ready(); micGranted = true; mic.stop(); toast('Microphone on. Hold to talk.'); }
      catch { toast('Microphone blocked. Allow it in site settings.'); }
      return;
    }
  }
  buzz(12);
  openSocket(r);
  try {
    await mic.ready();
  } catch (e) {
    r.failed = r.ended = true; // a socket still waiting for its ticket won't open
    r.ws?.close();
    if (rec === r) { rec = null; setRec('idle'); }
    micGranted = e.name !== 'NotAllowedError';
    toast(micGranted ? 'Could not open the microphone' : 'Microphone blocked. Allow it in site settings.');
    return;
  }
  if (rec !== r) { mic.stop(); return; } // cancelled while the mic was opening
  mic.start((pcm, level) => {
    if (rec !== r || r.ended) return;
    const buf = pcm.buffer;
    r.frames.push(buf);
    r.samples += pcm.length;
    if (r.ready && r.ws.readyState === 1) r.ws.send(buf);
    $('halo').style.setProperty('--lvl', Math.min(1, level * 2.2).toFixed(3));
    const s = Math.floor(r.samples / 16000);
    $('timer').textContent = `${Math.floor(s / 60)}:${String(s % 60).padStart(2, '0')}`;
    if (s >= MAX_SECONDS) finishRecording();
  });
  navigator.wakeLock?.request('screen').then(l => { wakeLock = l; }).catch(() => {});
  if (r.releasedEarly) setRec('locked');
  else setRec(press?.cancelArmed ? 'cancel' : 'recording');
}

async function cancelRecording() {
  const r = rec;
  if (!r) return;
  r.ended = true;
  rec = null;
  await mic.stop();
  if (r.ws?.readyState === 1) r.ws.send(JSON.stringify({ type: 'cancel' }));
  r.ws?.close();
  wakeLock?.release().catch(() => {}); wakeLock = null;
  setRec('idle');
  showLive('');
  buzz(20);
}

async function finishRecording() {
  const r = rec;
  if (!r || r.ended || r.finishing) return;
  r.finishing = true; // claimed before the await below, so a second stop can't finalize twice
  setRec('processing');
  await mic.stop();
  if (rec !== r) return; // discarded while the mic was stopping; cancelRecording cleaned up
  r.ended = true;
  wakeLock?.release().catch(() => {}); wakeLock = null;
  buzz(8);
  if (r.samples < 16000 * 0.3) {
    r.ws?.close();
    rec = null; setRec('idle'); showLive('');
    toast('Too short. Hold while you speak.');
    return;
  }
  let result = null;
  if (r.ready && r.ws.readyState === 1) r.ws.send(JSON.stringify({ type: 'end' }));
  else if (!r.failed) {
    // Still connecting: give it a moment, then fall back to uploading the take.
    try { await withTimeout(r.isReady, 6000); } catch { r.failed = true; r.ws?.close(); }
  }
  if (!r.failed) {
    try { result = await withTimeout(r.final, 90_000); } catch (e) { console.warn('dictate socket:', e); }
  }
  if (!result) {
    try { result = await upload(r); } catch (e) {
      console.warn('upload:', e);
      rec = null; setRec('idle'); showLive('');
      showFailure(r);
      return;
    }
  }
  rec = null;
  setRec('idle');
  showLive('');
  if (result.empty || !result.text) { toast("Didn't catch anything"); buzz([20, 60, 20]); return; }
  openResult({ id: result.dictation_id, result, text: result.text, style: r.style, fresh: true });
}

const withTimeout = (p, ms) => Promise.race([p, new Promise((_, rej) => setTimeout(() => rej(new Error('timeout')), ms))]);

function wav(frames, samples) {
  const out = new DataView(new ArrayBuffer(44 + samples * 2));
  const str = (o, s) => [...s].forEach((c, i) => out.setUint8(o + i, c.charCodeAt(0)));
  str(0, 'RIFF'); out.setUint32(4, 36 + samples * 2, true); str(8, 'WAVE'); str(12, 'fmt ');
  out.setUint32(16, 16, true); out.setUint16(20, 1, true); out.setUint16(22, 1, true);
  out.setUint32(24, 16000, true); out.setUint32(28, 32000, true); out.setUint16(32, 2, true); out.setUint16(34, 16, true);
  str(36, 'data'); out.setUint32(40, samples * 2, true);
  let o = 44;
  for (const f of frames) { new Uint8Array(out.buffer, o, f.byteLength).set(new Uint8Array(f)); o += f.byteLength; }
  return new Blob([out.buffer], { type: 'audio/wav' });
}

async function upload(r) {
  const form = new FormData();
  form.append('file', wav(r.frames, r.samples), 'phone.wav');
  form.append('save', 'true');
  form.append('llm', String(prefs.llm));
  form.append('app_hint', 'phone');
  if (r.profile) form.append('profile', r.profile);
  form.append('style', r.style === 'verbatim' ? 'auto' : r.style);
  return api('/transcribe', { method: 'POST', body: form, timeout: 120_000 });
}

/** Takes that never reached the server; listed at the top of history until sent. */
const unsent = [];

/** The server was unreachable: keep the take and offer to send it again. */
function showFailure(r) {
  current = null;
  if (!unsent.includes(r)) unsent.push(r);
  $('text').className = 'text';
  $('text').innerHTML = `<div class="err"><b>Couldn't reach transcriptd</b>Your ${secs(r.samples / 16)} recording is kept on this screen. Check Tailscale, then retry.</div>`;
  $('resultMeta').textContent = 'not sent';
  $('copyBtn').onclick = retry;
  $('copyBtn').innerHTML = ICONS.retry;
  $('copyBtn').setAttribute('aria-label', 'Retry');
  go('result');
  buzz([30, 80, 30]);
  async function retry() {
    if (r.retrying) return; // a double tap must not upload twice
    r.retrying = true;
    $('text').classList.add('busy');
    try {
      const result = await upload(r);
      const i = unsent.indexOf(r);
      if (i >= 0) unsent.splice(i, 1);
      if (result.empty || !result.text) { toast("Didn't catch anything"); go('home'); return; }
      openResult({ id: result.dictation_id, result, text: result.text, style: r.style, fresh: true });
    } catch {
      $('text').classList.remove('busy');
      toast('Still offline');
    } finally {
      r.retrying = false;
    }
  }
}

// -- press handling: hold = push-to-talk, tap = hands-free, slide away = cancel ---------------

let press = null;

function onDown(e) {
  if (e.button !== 0) return;
  e.preventDefault();
  if (rec?.state === 'locked') { press = { stopTap: true }; return; }
  if (rec) return; // starting or processing
  if (view !== 'home') go('home');
  e.currentTarget.setPointerCapture?.(e.pointerId);
  press = { x: e.clientX, y: e.clientY, t: performance.now(), cancelArmed: false };
  startRecording();
}

function onMove(e) {
  if (!press || press.stopTap || !rec || rec.state === 'locked') return;
  const far = Math.hypot(e.clientX - press.x, e.clientY - press.y) > CANCEL_PX;
  if (far !== press.cancelArmed) {
    press.cancelArmed = far;
    if (rec.state === 'recording' || rec.state === 'cancel') { setRec(far ? 'cancel' : 'recording'); buzz(far ? 15 : 5); }
  }
}

function onUp() {
  const p = press;
  press = null;
  if (!p) return;
  if (p.stopTap) { finishRecording(); return; }
  if (!rec) return;
  if (p.cancelArmed) { cancelRecording(); return; }
  if (performance.now() - p.t < HOLD_MS) {
    if (rec.state === 'starting') rec.releasedEarly = true;
    else { setRec('locked'); buzz(6); }
    return;
  }
  if (rec.state === 'starting') { waitThenFinish(rec); return; }
  finishRecording();
}

// Held long enough but the mic was still opening (slow first start): finish once it is live.
function waitThenFinish(r) {
  const tick = () => {
    if (rec !== r) return;
    if (r.state === 'starting') return setTimeout(tick, 30);
    finishRecording();
  };
  tick();
}

for (const b of [$('mic'), $('resultMic')]) {
  b.addEventListener('pointerdown', onDown);
  b.addEventListener('contextmenu', e => e.preventDefault());
}
// On the window: the result view's mic is hidden mid-press, which drops pointer capture.
addEventListener('pointermove', onMove);
addEventListener('pointerup', onUp);
// The system took the gesture (a call, a scroll): discard the hold, never submit it.
addEventListener('pointercancel', () => {
  const p = press;
  press = null;
  if (p && !p.stopTap && rec) cancelRecording();
});
$('cancelBtn').onclick = () => cancelRecording();

// -- result ---------------------------------------------------------------------------------

let current = null; // { id, result, text, style, verbatim }

function openResult(c) {
  current = c;
  $('copyBtn').onclick = () => copyCurrent(false);
  $('copyBtn').innerHTML = ICONS.copy;
  $('copyBtn').setAttribute('aria-label', 'Copy');
  renderResult();
  go('result');
  if (c.fresh) {
    buzz(10);
    if (prefs.autoCopy) copyCurrent(true);
  }
}

function shownText() {
  return current.verbatim ? current.result.raw_text : current.text;
}

async function copyCurrent(auto) {
  const ok = await copyText(shownText(), auto);
  if (ok) {
    const meta = $('resultMeta');
    meta.textContent = 'Copied to clipboard';
    meta.classList.add('flash');
    $('copyBtn').innerHTML = ICONS.check;
    clearTimeout(copyCurrent.timer);
    copyCurrent.timer = setTimeout(() => { $('copyBtn').innerHTML = ICONS.copy; meta.classList.remove('flash'); renderMeta(); }, 1600);
  } else if (auto) {
    toast('Tap copy to put it on the clipboard');
  }
}

function renderResult() {
  const { result } = current;
  const el = $('text');
  el.className = current.verbatim ? 'text verbatim' : 'text';
  $('styleLabel').textContent = current.verbatim ? 'Verbatim' : styleLabel(result.style || current.style);
  if (!$('resultMeta').classList.contains('flash')) renderMeta();
  if (current.verbatim) {
    el.textContent = result.raw_text;
  } else {
    const text = current.text;
    const words = annotate(result, text);
    let html = '', at = 0;
    words.forEach((w, i) => {
      html += esc(text.slice(at, w.start));
      at = w.end;
      if (!w.key) { html += esc(w.text); return; }
      const cls = ['w', w.low ? 'low' : tone(w), current.fixed?.has(i) ? 'fixed' : ''].filter(Boolean).join(' ');
      html += `<span class="${cls}" data-i="${i}">${esc(w.text)}</span>`;
    });
    html += esc(text.slice(at));
    el.innerHTML = html;
    current.words = words;
  }
  requestAnimationFrame(fitText);
}

function renderMeta() {
  if (!current) return;
  const r = current.result;
  $('resultMeta').textContent = [r.duration_ms ? secs(r.duration_ms) : '', r.created_at ? ago(r.created_at) : '', r.profile]
    .filter(Boolean).join(' · ');
}

/** Largest type that fits without scrolling, down to a readable floor. */
function fitText() {
  const wrap = $('textWrap'), el = $('text');
  if (view !== 'result' || !wrap.clientHeight) return;
  const tall = innerHeight >= 640;
  let size = tall ? 30 : Math.round(Math.min(34, Math.max(24, innerWidth / 13)));
  const floor = tall ? 18 : 16;
  el.style.setProperty('--fs', `${size}px`);
  while (size > floor && wrap.scrollHeight > wrap.clientHeight + 1) {
    size -= 1;
    el.style.setProperty('--fs', `${size}px`);
  }
}
addEventListener('resize', () => requestAnimationFrame(fitText));

$('text').addEventListener('click', e => {
  const span = e.target.closest('.w');
  if (!span || !current || getSelection()?.toString()) return;
  openFix(Number(span.dataset.i));
});

$('shareBtn').onclick = async () => {
  if (!current) return;
  if (navigator.share) navigator.share({ text: shownText() }).catch(() => {});
  else copyCurrent(false);
};

$('deleteBtn').onclick = async () => {
  if (!current?.id) { go('home'); return; }
  const gone = current;
  try { await api(`/dictations/${gone.id}`, { method: 'DELETE' }); } catch { toast('Delete failed'); return; }
  current = null;
  go('home');
  toast('Deleted');
};

$('styleBtn').onclick = () => {
  if (!current) return;
  const active = current.verbatim ? 'verbatim' : current.result.style || current.style;
  openOptions('Clean up as', [...styles(), { name: 'verbatim', label: 'Verbatim' }].map(s => ({
    label: s.label, note: s.name === 'verbatim' ? 'exactly what was heard' : '', checked: s.name === active,
    run: () => restyle(s.name),
  })));
};

async function restyle(name) {
  const c = current;
  if (name !== 'verbatim') { prefs.style = name; save(); renderStyleBar(); }
  if (name === 'verbatim') {
    c.verbatim = true;
    renderResult();
    copyCurrent(true);
    if (c.id) api(`/dictations/${c.id}/final`, { json: { final_text: c.result.raw_text } }).catch(() => {});
    return;
  }
  const wasVerbatim = c.verbatim;
  c.verbatim = false;
  if (!c.id || (name === (c.result.style || c.style) && !c.fixed?.size)) {
    renderResult();
    copyCurrent(true);
    if (c.id && wasVerbatim) api(`/dictations/${c.id}/final`, { json: { final_text: c.text } }).catch(() => {});
    return;
  }
  $('text').classList.add('busy');
  try {
    const result = await api(`/dictations/${c.id}/restyle`, { json: { style: name, llm: prefs.llm }, timeout: 60_000 });
    if (current !== c) return;
    c.result = { ...result, created_at: c.result.created_at };
    c.text = result.text;
    c.style = name;
    c.fixed = null;
    renderResult();
    copyCurrent(true);
  } catch {
    toast('Restyle failed');
  } finally {
    $('text').classList.remove('busy');
  }
}

// -- fix a word -----------------------------------------------------------------------------

function openFix(i) {
  const w = current.words[i];
  const why = w.low ? 'The model was unsure here.' : w.edits.map(e => describe(e.edit))[0] || '';
  const heard = w.heard && w.heard.toLowerCase() !== w.text.toLowerCase();
  openSheet(`
    <h2>Fix word</h2>
    <p class="heard">${heard ? `Heard <code>${esc(w.heard)}</code>` : ''}${why ? `<span class="why">${esc(why)}</span>` : ''}</p>
    <form id="fixForm">
      <input class="fix" id="fixInput" value="${esc(w.text)}" autocomplete="off" autocapitalize="off" spellcheck="false" enterkeyhint="done">
      <div class="btns">
        ${heard ? '<button type="button" id="useHeard">Use heard</button>' : ''}
        <button type="submit" class="primary">Save</button>
      </div>
    </form>`);
  const input = $('fixInput');
  input.focus();
  input.select();
  $('useHeard')?.addEventListener('click', () => applyFix(i, w.heard));
  $('fixForm').onsubmit = e => { e.preventDefault(); applyFix(i, input.value.trim()); };
}

async function applyFix(i, replacement) {
  closeSheet();
  const c = current;
  const w = c.words[i];
  if (!replacement || replacement === w.text) return;
  // Keep the punctuation that was glued to the word unless the user typed their own.
  const lead = w.text.match(/^[\p{P}\p{S}]*/u)[0], trail = w.text.match(/[\p{P}\p{S}]*$/u)[0];
  const bare = replacement.replace(/^[\p{P}\p{S}]+|[\p{P}\p{S}]+$/gu, '');
  const surface = bare === replacement ? lead + replacement + trail : replacement;
  c.text = c.text.slice(0, w.start) + surface + c.text.slice(w.end);
  c.fixed ??= new Set();
  c.fixed.add(i);
  renderResult();
  copyCurrent(true);
  if (!c.id) return;
  api(`/dictations/${c.id}/final`, { json: { final_text: c.text } }).catch(() => {});
  try {
    const r = await api('/corrections', { json: { heard: w.heard, meant: bare || replacement, dictation_id: c.id, source: 'phone_edit' } });
    if (r.learned) toast(`Learned “${r.term}”`);
    else if (r.suggest) toast(`Remember “${r.term}”?`, {
      label: 'Add',
      run: () => api('/vocab', { json: { term: r.term, heard_as: [r.heard_as], added_from: 'phone_edit' } })
        .then(() => toast(`Added “${r.term}”`)).catch(() => toast('Could not add it')),
    });
  } catch {}
}

// -- sheets ---------------------------------------------------------------------------------

function openSheet(html) {
  $('sheetBody').innerHTML = html;
  $('sheet').hidden = false;
}
function closeSheet() { $('sheet').hidden = true; }
$('sheet').addEventListener('click', e => { if (e.target.dataset.close !== undefined) closeSheet(); });

function openOptions(title, options) {
  openSheet(`<h2>${esc(title)}</h2>` + options.map((o, n) =>
    `<button class="opt" role="menuitemradio" aria-checked="${o.checked}" data-n="${n}"><span>${esc(o.label)}</span>${o.note ? `<small>${esc(o.note)}</small>` : ''}</button>`).join(''));
  for (const b of $('sheetBody').querySelectorAll('.opt')) b.onclick = () => { closeSheet(); buzz(4); options[Number(b.dataset.n)].run(); };
}

// -- history --------------------------------------------------------------------------------

const historyRows = new Map();

async function loadHistory() {
  const list = $('historyList');
  if (!list.children.length) list.innerHTML = '<li class="empty">Loading…</li>';
  let rows;
  const pending = unsent.map((r, i) => `<li><button data-unsent="${i}"><span class="t">Not sent · tap to retry</span><span class="m">${secs(r.samples / 16)} recording</span></button></li>`).join('');
  try { rows = await api('/dictations?limit=60'); } catch { list.innerHTML = pending + '<li class="empty">Server unreachable</li>'; return; }
  historyRows.clear();
  for (const d of rows) historyRows.set(String(d.id), d);
  if (!rows.length && !unsent.length) { list.innerHTML = '<li class="empty">Nothing yet. Hold the mic and talk.</li>'; return; }
  list.innerHTML = pending + rows.map(d => `<li><button data-id="${d.id}"><span class="t">${esc(d.text || d.raw_text || '…')}</span><span class="m">${esc(ago(d.created_at))}${d.duration_ms ? ' · ' + secs(d.duration_ms) : ''}${d.app_hint ? ' · ' + esc(d.app_hint) : ''}</span></button></li>`).join('');
}

$('historyList').addEventListener('click', async e => {
  const u = e.target.closest('button[data-unsent]');
  if (u) { showFailure(unsent[+u.dataset.unsent]); return; }
  const b = e.target.closest('button[data-id]');
  if (!b) return;
  try {
    const d = await api(`/dictations/${b.dataset.id}`);
    const saved = historyRows.get(b.dataset.id)?.text || d.result.text;
    const result = { ...d.result, created_at: d.created_at };
    const verbatim = saved === result.raw_text && saved !== result.text;
    openResult({ id: d.id, result, text: verbatim ? result.text : saved, style: result.style || 'auto', verbatim });
  } catch { toast('Could not open it'); }
});

// -- settings / pairing ---------------------------------------------------------------------

function renderSettings() {
  $('optCopy').checked = prefs.autoCopy;
  $('optHaptics').checked = prefs.haptics;
  $('optLlm').checked = prefs.llm;
  const h = health;
  $('serverInfo').innerHTML = h
    ? `<b>${esc(location.host)}</b><br>transcriptd ${esc(h.version)} · model <b>${esc(prefs.profile)}</b><br>`
      + (prefs.llm && h.llm_configured ? 'LLM polish on, checked by the guard' : 'Rule-based cleanup only, checked by the guard')
      + ` · ${h.vocab_terms} vocabulary terms`
    : `<b>${esc(location.host)}</b><br>Not reachable right now.`;
}
$('optCopy').onchange = e => { prefs.autoCopy = e.target.checked; save(); };
$('optHaptics').onchange = e => { prefs.haptics = e.target.checked; save(); };
$('optLlm').onchange = e => { prefs.llm = e.target.checked; save(); renderSettings(); };
$('unpairBtn').onclick = () => unpair('Unpaired');

let installPrompt = null;
addEventListener('beforeinstallprompt', e => { e.preventDefault(); installPrompt = e; $('installBtn').hidden = false; });
$('installBtn').onclick = async () => { if (!installPrompt) return; installPrompt.prompt(); await installPrompt.userChoice; installPrompt = null; $('installBtn').hidden = true; };

function unpair(message) {
  prefs.token = '';
  save();
  mic.release();
  history.replaceState(null, '');
  render('pair');
  if (message) toast(message);
}

async function pair(token) {
  prefs.token = token.trim();
  save();
  render('home');
  await refreshHealth();
  if (health) toast('Paired');
}

$('pairForm').onsubmit = e => { e.preventDefault(); if ($('tokenInput').value.trim()) pair($('tokenInput').value); };

// -- boot -----------------------------------------------------------------------------------

paintIcons();
setRec('idle');
renderStyleBar();
const fromLink = new URLSearchParams(location.hash.slice(1)).get('t');
if (fromLink) {
  history.replaceState(null, '', location.pathname); // keep the token out of history and screenshots
  pair(fromLink);
} else if (prefs.token) {
  history.replaceState(null, '');
  render('home');
  refreshHealth();
} else {
  render('pair');
}
if ('serviceWorker' in navigator) navigator.serviceWorker.register('sw.js').catch(() => {});
