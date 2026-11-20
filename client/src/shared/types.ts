export interface InjectOptions {
  method: 'clipboard' | 'type'; paste_chord: string | null;
  restore_clipboard: boolean; restore_delay_ms: number; trailing_space: boolean;
}
export interface Settings {
  serverUrl: string; token: string; profile: string | null;
  hotkey: string; correctionHotkey: string | null; hotkeyMode: 'hold' | 'toggle';
  autoInject: boolean; inject: InjectOptions; micDevice: string | null;
  loopbackDevice: string | null; overlayEnabled: boolean; showPartials: boolean; startAtLogin: boolean;
  /** When the overlay keeps the result on screen after a dictation. */
  resultDisplay: 'always' | 'flagged' | 'never';
}
export interface AgentConfig {
  server_url: string; token: string; profile: string | null; language: string | null;
  hotkey: string; correction_hotkey: string | null; hotkey_mode: 'hold' | 'toggle';
  auto_inject: boolean; inject: InjectOptions; mic_device: string | null;
  loopback_device: string | null; partials: boolean;
}
export interface Word { text: string; start_ms: number; end_ms: number; conf: number }
export interface CleanupEdit { kind: string; raw: string; cleaned: string; accepted: boolean; reason: string; raw_span: [number, number] }
export interface DictationResult {
  text: string; raw_text: string; words: Word[]; edits: CleanupEdit[];
  replacements: Array<Record<string, unknown>>; low_confidence: Array<{index: number; text: string; conf: number}>;
  dictation_id?: number | null; profile?: string; language?: string | null;
}
export interface DictationRow { id: number; created_at: string; profile: string; language: string | null; raw_text: string; text: string; duration_ms: number; app_hint: string | null }
export interface DictationDetail { id: number; created_at: string; result: DictationResult }
export interface Health { ok: boolean; version: string; default_profile: string; profiles: Record<string, Record<string, unknown>>; loaded: unknown; vocab_terms: number; llm_configured: boolean }
export interface VocabInput { term: string; aliases: string[]; heard_as: string[]; language?: string | null; added_from?: string }
export interface VocabTerm extends VocabInput { id: number }
export interface Correction { heard: string; meant: string; dictation_id?: number | null; source?: string }
export interface CorrectionReply { suggest: boolean; term?: string; heard_as?: string; reason?: string; times_seen?: number; learned?: boolean }
export interface Segment { id?: number; channel: string; speaker?: string; start_ms: number; end_ms: number; text: string; words?: Word[]; final?: boolean; live?: boolean; raw_text?: string }
export interface Meeting { id: number; title: string; started_at: string; ended_at: string | null; status: string; profile: string; has_audio: boolean; summary: string; notes: string; segments?: Segment[] }
export interface Devices { inputs: Array<{name: string; default: boolean}>; outputs: Array<{name: string; default: boolean}> }
export interface Hello { version: string; platform: string; hotkey_backend: string; inject_backend: string; readback_backend?: string }
export type AgentEvent =
  | ({type: 'hello'} & Hello)
  | {type: 'configured'; warnings: string[]}
  | {type: 'ptt.down' | 'ptt.up'; source?: string}
  | {type: 'level'; db: number}
  | {type: 'partial'; text: string}
  | {type: 'final'; result: DictationResult; injected: boolean}
  | {type: 'injected'; chars: number; method: string}
  | {type: 'clipboard' | 'selection'; text?: string | null}
  | {type: 'corrections'; dictation_id: number | null; app: string; corrections: Array<{heard: string; meant: string}>}
  | ({type: 'devices'} & Devices)
  | {type: 'meeting.started'; meeting_id?: number | null; wav_path: string}
  | {type: 'meeting.level'; me_db: number; them_db: number}
  | {type: 'meeting.segment'; meeting_id?: number | null; segment: Segment}
  | {type: 'meeting.stopped'; meeting_id?: number | null; wav_path: string; duration_ms: number}
  | {type: 'error'; message: string; fatal: boolean}
  | {type: 'pong'};
export type AgentCommand =
  | {type: 'configure'; config: AgentConfig}
  | {type: 'ptt.start' | 'ptt.stop' | 'ptt.toggle' | 'ptt.cancel' | 'devices.list' | 'clipboard.read' | 'selection.read' | 'meeting.stop' | 'ping' | 'quit'}
  | {type: 'inject'; text: string}
  | {type: 'meeting.start'; meeting_id?: number; title?: string};
export type AgentStatus = 'starting' | 'online' | 'offline' | 'restarting';
export interface RuntimeState { agentStatus: AgentStatus; hello: Hello | null; warnings: string[]; recording: boolean; activeMeetingId: number | null; devices: Devices; lastResult: DictationResult | null }
export interface VocabSuggestion { term: string; heard_as: string }
export type Page = 'dictations' | 'meetings' | 'vocabulary' | 'settings';
export type ClientEvent =
  | {type: 'agent'; event: AgentEvent}
  | {type: 'state'; state: RuntimeState}
  | {type: 'settings'; settings: Settings}
  | {type: 'navigate'; page: Page}
  | {type: 'suggestion'; suggestion: VocabSuggestion | null}
  | {type: 'notice'; message: string};
export interface ServerApi {
  health(connection?: {serverUrl: string; token: string}): Promise<Health>;
  dictations(q?: string): Promise<DictationRow[]>;
  dictation(id: number): Promise<DictationDetail>;
  setFinal(id: number, finalText: string): Promise<{ok: boolean}>;
  vocab(): Promise<VocabTerm[]>;
  addVocab(input: VocabInput): Promise<VocabTerm>;
  updateVocab(id: number, input: VocabInput): Promise<VocabTerm>;
  deleteVocab(id: number): Promise<void>;
  importVocab(text: string): Promise<{added: number}>;
  exportVocab(): Promise<string>;
  suppress(word: string): Promise<void>;
  correction(input: Correction): Promise<CorrectionReply>;
  meetings(q?: string): Promise<Meeting[]>;
  createMeeting(input: {title: string; profile?: string | null}): Promise<Meeting>;
  meeting(id: number): Promise<Meeting>;
  patchMeeting(id: number, input: {title?: string; notes?: string}): Promise<Meeting>;
  deleteMeeting(id: number): Promise<void>;
  transcript(id: number, format?: 'md' | 'txt'): Promise<string>;
  summarize(id: number): Promise<{summary: string}>;
  processMeeting(id: number): Promise<{ok: boolean}>;
}
export interface DesktopApi {
  server: ServerApi;
  getSettings(): Promise<Settings>;
  saveSettings(settings: Settings): Promise<Settings>;
  getState(): Promise<RuntimeState>;
  restartAgent(): Promise<void>;
  listDevices(): Promise<void>;
  inject(text: string): Promise<void>;
  startMeeting(title?: string): Promise<Meeting>;
  stopMeeting(): Promise<void>;
  correct(input: Correction & {finalText?: string}): Promise<CorrectionReply>;
  getSuggestion(): Promise<VocabSuggestion | null>;
  resolveSuggestion(action: 'add' | 'replace' | 'skip' | 'never'): Promise<void>;
  overlayHold(hold: boolean): Promise<void>;
  openMain(page?: Page): Promise<void>;
  saveText(suggestedName: string, text: string): Promise<boolean>;
  onEvent(listener: (event: ClientEvent) => void): () => void;
}
