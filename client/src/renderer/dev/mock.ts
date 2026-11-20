// Dev-only fake bridge so the renderer can be previewed in a browser.
// Query parameters pick a scenario:
//   overlay.html?phase=idle|recording|processing|result|unpasted|error
//   index.html?page=meetings&recording=1&warn=1
//   vocab.html?empty=1
import type { ClientEvent, CleanupEdit, DesktopApi, DictationDetail, DictationResult, DictationRow, Health, Meeting, RuntimeState, Segment, Settings, VocabInput, VocabTerm, Word } from '../../shared/types';

const params = new URLSearchParams(location.search);
const wait = (ms = 180) => new Promise(resolve => setTimeout(resolve, ms));
const ago = (minutes: number) => new Date(Date.now() - minutes * 60_000).toISOString();

function words(raw: string, low: number[] = []): Word[] {
  return raw.split(/\s+/).map((text, i) => ({ text, start_ms: i * 320, end_ms: i * 320 + 300, conf: low.includes(i) ? .38 : .94 }));
}
const e = (kind: string, raw: string, cleaned: string, span: [number, number], reason: string, accepted = true): CleanupEdit => ({ kind, raw, cleaned, raw_span: span, reason, accepted });
function result(id: number, raw: string, text: string, edits: CleanupEdit[], low: number[], language: string, profile = 'default'): DictationResult {
  const w = words(raw, low);
  return { dictation_id: id, raw_text: raw, text, words: w, edits, replacements: [], low_confidence: low.map(index => ({ index, text: w[index].text, conf: .38 })), language, profile };
}

const results: Record<number, DictationResult> = {
  41: result(41, 'um so the idea is that we ship the cooper netties operator by by friday and then uh we freeze the branch',
    'So the idea is that we ship the Kubernetes operator by Friday, and then we freeze the branch.',
    [e('delete', 'um', '', [0, 1], 'filler'), e('replace', 'cooper netties', 'Kubernetes', [9, 11], 'vocab'), e('delete', 'by', '', [12, 13], 'stutter'), e('delete', 'uh', '', [17, 18], 'filler')],
    [11], 'en'),
  40: result(40, 'कल की मीटिंग में हम deployment pipeline के बारे में बात करेंगे और uh hyper land वाला issue भी देखेंगे',
    'कल की मीटिंग में हम deployment pipeline के बारे में बात करेंगे और Hyprland वाला issue भी देखेंगे।',
    [e('delete', 'uh', '', [13, 14], 'filler'), e('replace', 'hyper land', 'Hyprland', [14, 16], 'vocab')],
    [2], 'hi', 'indic'),
  39: result(39, 'मी उद्या ऑफिसला नाही येणार कारण doctor ची appointment आहे',
    'मी उद्या ऑफिसला नाही येणार कारण doctor ची appointment आहे.',
    [e('delete', 'नाही', '', [3, 4], 'protected word deleted', false)], [], 'mr', 'indic'),
  38: result(38, 'we should co ordinate with the the ops team before we never mind just send it',
    'We should coordinate with the ops team before we… never mind, just send it.',
    [e('replace', 'co ordinate', 'coordinate', [2, 4], 'spelling variant'), e('delete', 'the', '', [5, 6], 'stutter'), e('delete', 'never', '', [10, 11], 'protected word deleted', false)], [8], 'en'),
  37: result(37, 'sounds good lets talk tomorrow', 'Sounds good, lets talk tomorrow.', [], [], 'en'),
};
const rows: DictationRow[] = [
  { id: 41, created_at: ago(3), profile: 'default', language: 'en', raw_text: results[41].raw_text, text: results[41].text, duration_ms: 7400, app_hint: 'Slack' },
  { id: 40, created_at: ago(26), profile: 'indic', language: 'hi', raw_text: results[40].raw_text, text: results[40].text, duration_ms: 6100, app_hint: 'VS Code' },
  { id: 39, created_at: ago(60 * 20), profile: 'indic', language: 'mr', raw_text: results[39].raw_text, text: results[39].text, duration_ms: 4200, app_hint: 'WhatsApp' },
  { id: 38, created_at: ago(60 * 26), profile: 'default', language: 'en', raw_text: results[38].raw_text, text: results[38].text, duration_ms: 5600, app_hint: null },
  { id: 37, created_at: ago(60 * 24 * 4), profile: 'default', language: 'en', raw_text: results[37].raw_text, text: results[37].text, duration_ms: 1900, app_hint: 'Firefox' },
];

const segments: Segment[] = [
  { id: 1, channel: 'me', start_ms: 4_000, end_ms: 9_800, text: 'Okay, let’s start. The Kubernetes operator is ready for review, I pushed it last night.', final: true },
  { id: 2, channel: 'them', speaker: 'Speaker 1', start_ms: 10_400, end_ms: 17_200, text: 'Nice. मैंने कल देखा था, लेकिन Helm chart में values file अभी भी पुरानी है।', final: true },
  { id: 3, channel: 'them', speaker: 'Speaker 1', start_ms: 17_600, end_ms: 21_000, text: 'Can you update that before Friday?', final: true },
  { id: 4, channel: 'me', start_ms: 21_500, end_ms: 26_900, text: 'Yes. I will also add the Hyprland note to the README so people stop asking.', final: true },
  { id: 5, channel: 'them', speaker: 'Speaker 2', start_ms: 27_300, end_ms: 34_000, text: 'आणि पुढच्या आठवड्यात आपण load test करू, पण staging वर नाही, फक्त perf cluster वर.', final: true },
  { id: 6, channel: 'me', start_ms: 34_500, end_ms: 37_100, text: 'Agreed. Let’s freeze the branch on Thursday.', final: true },
];
const meetings: Meeting[] = [
  { id: 8, title: 'Design review: overlay', started_at: ago(12), ended_at: null, status: 'recording', profile: 'default', has_audio: false, summary: '', notes: '', segments: segments.slice(0, 2).map(s => ({ ...s, final: false })) },
  { id: 7, title: 'Weekly infra sync', started_at: ago(60 * 3), ended_at: ago(60 * 3 - 38), status: 'done', profile: 'indic', has_audio: true,
    summary: 'The Kubernetes operator is ready for review. Helm values need updating before Friday.\nLoad testing happens next week on the perf cluster only, not staging.\nBranch freeze is on Thursday.',
    notes: 'Ask Priya about perf cluster quota.', segments },
  { id: 6, title: 'Call with Ananya', started_at: ago(60 * 27), ended_at: ago(60 * 27 - 21), status: 'processing', profile: 'indic', has_audio: true, summary: '', notes: '', segments: [] },
  { id: 5, title: 'Standup', started_at: ago(60 * 24 * 5), ended_at: ago(60 * 24 * 5 - 14), status: 'done', profile: 'default', has_audio: true, summary: '', notes: '', segments: segments.slice(0, 3) },
];
let vocab: VocabTerm[] = [
  { id: 1, term: 'Kubernetes', aliases: ['k8s'], heard_as: ['cooper netties', 'kuber netis'], language: null, added_from: 'overlay' },
  { id: 2, term: 'Hyprland', aliases: [], heard_as: ['hyper land', 'hip land'], language: null, added_from: 'manual' },
  { id: 3, term: 'Parth', aliases: ['Parth Thakre'], heard_as: ['path', 'part'], language: null, added_from: 'manual' },
  { id: 4, term: 'transcriptd', aliases: [], heard_as: ['transcript d', 'transcript dee'], language: 'en', added_from: 'import' },
  { id: 5, term: 'नागपूर', aliases: ['Nagpur'], heard_as: ['नागपुर'], language: 'mr', added_from: 'correction' },
  { id: 6, term: 'Wispr Flow', aliases: [], heard_as: ['whisper flow'], language: 'en', added_from: 'manual' },
];

let settings: Settings = {
  serverUrl: 'http://127.0.0.1:7411', token: 'tdk_4f1c9e0b7a', profile: null, hotkey: 'ctrl+alt+space', correctionHotkey: 'ctrl+alt+shift+v', hotkeyMode: 'hold',
  autoInject: true, inject: { method: 'clipboard', paste_chord: null, restore_clipboard: true, restore_delay_ms: 400, trailing_space: true },
  micDevice: null, loopbackDevice: null, overlayEnabled: true, showPartials: true, startAtLogin: false, resultDisplay: 'never',
};
const state: RuntimeState = {
  agentStatus: params.get('agent') as RuntimeState['agentStatus'] ?? 'online',
  hello: { version: '0.3.1', platform: 'linux-x86_64', hotkey_backend: 'evdev', inject_backend: 'uinput + wl-clipboard' },
  warnings: params.has('warn') ? ['Input group missing: global hotkeys need the udev rule. See packaging/70-transcriptd.rules.'] : [],
  recording: params.get('phase') === 'recording', activeMeetingId: params.has('recording') ? 8 : null,
  devices: { inputs: [{ name: 'Built-in Audio Analog Stereo', default: true }, { name: 'Blue Yeti', default: false }], outputs: [{ name: 'alsa_output.pci-0000_00_1f.3.analog-stereo.monitor', default: true }] },
  lastResult: params.get('phase') === 'idle' ? null : results[params.get('dictation') ? Number(params.get('dictation')) : 41],
};
const health: Health = { ok: true, version: '0.3.1', default_profile: 'default', profiles: { default: {}, indic: {}, 'meeting-large': {} }, loaded: { asr: 'parakeet-tdt-0.6b-v3', indic: 'indicconformer-hi-mr' }, vocab_terms: vocab.length, llm_configured: true };

const listeners = new Set<(event: ClientEvent) => void>();
const emit = (event: ClientEvent) => listeners.forEach(listener => listener(event));

const bridge: DesktopApi = {
  server: {
    health: async () => { await wait(500); return health; },
    dictations: async q => { await wait(); return q ? rows.filter(r => `${r.text} ${r.raw_text}`.toLowerCase().includes(q.toLowerCase())) : rows; },
    dictation: async id => { await wait(); const row = rows.find(r => r.id === id)!; return { id, created_at: row.created_at, result: results[id] } satisfies DictationDetail; },
    setFinal: async () => ({ ok: true }),
    vocab: async () => { await wait(); return vocab; },
    addVocab: async (input: VocabInput) => { const term = { ...input, id: Date.now() }; vocab = [...vocab, term]; return term; },
    updateVocab: async (id, input) => { const term = { ...input, id }; vocab = vocab.map(v => v.id === id ? term : v); return term; },
    deleteVocab: async id => { vocab = vocab.filter(v => v.id !== id); },
    importVocab: async text => ({ added: text.split('\n').filter(Boolean).length }),
    exportVocab: async () => vocab.map(v => v.term).join('\n'),
    suppress: async () => {},
    correction: async () => ({ suggest: false }),
    meetings: async q => { await wait(); return q ? meetings.filter(m => m.title.toLowerCase().includes(q.toLowerCase())) : meetings; },
    createMeeting: async input => { const m: Meeting = { id: Date.now(), title: input.title, started_at: new Date().toISOString(), ended_at: null, status: 'notes', profile: 'default', has_audio: false, summary: '', notes: '', segments: [] }; meetings.unshift(m); return m; },
    meeting: async id => { await wait(); return meetings.find(m => m.id === id)!; },
    patchMeeting: async (id, input) => { const m = meetings.find(x => x.id === id)!; Object.assign(m, input); return m; },
    deleteMeeting: async () => {},
    transcript: async () => '# transcript',
    summarize: async () => { await wait(800); return { summary: 'Summary text.' }; },
    processMeeting: async () => ({ ok: true }),
  },
  getSettings: async () => settings,
  saveSettings: async next => { settings = next; return next; },
  getState: async () => state,
  restartAgent: async () => {},
  listDevices: async () => {},
  inject: async () => { await wait(1500); },
  startMeeting: async title => bridge.server.createMeeting({ title: title ?? 'Untitled meeting' }),
  stopMeeting: async () => {},
  correct: async () => { await wait(400); return { suggest: true, term: 'Kubernetes', heard_as: 'cooper netties' }; },
  getSuggestion: async () => params.has('empty') ? null : { term: 'Kubernetes', heard_as: 'cooper netties' },
  resolveSuggestion: async () => { await wait(300); },
  overlayHold: async () => {},
  openMain: async () => {},
  saveText: async () => true,
  onEvent: listener => { listeners.add(listener); return () => listeners.delete(listener); },
};
window.transcriptd = bridge;

// Overlay scenarios.
const phase = params.get('phase');
if (location.pathname.endsWith('overlay.html') && phase) {
  const agent = (event: Extract<ClientEvent, { type: 'agent' }>['event']) => emit({ type: 'agent', event });
  setTimeout(() => {
    if (phase === 'recording' || phase === 'processing') {
      agent({ type: 'ptt.down' });
      const partial = params.get('lang') === 'hi' ? 'कल की मीटिंग में हम deployment pipeline के बारे में बात करेंगे और' : 'so the idea is that we ship the Kubernetes operator by Friday and then';
      agent({ type: 'partial', text: partial });
      let t = 0;
      const timer = setInterval(() => { t++; agent({ type: 'level', db: -38 + Math.sin(t / 2) * 14 + Math.random() * 6 }); }, 120);
      if (params.has('still')) setTimeout(() => clearInterval(timer), 400);
      if (phase === 'processing') { clearInterval(timer); agent({ type: 'ptt.up' }); }
    }
    if (phase === 'result' || phase === 'unpasted') agent({ type: 'final', result: state.lastResult!, injected: phase === 'result' });
    if (phase === 'error') emit({ type: 'agent', event: { type: 'error', message: 'The server did not respond within 20 seconds. Check that it is running.', fatal: false } });
  }, 700);
}
