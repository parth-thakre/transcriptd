import { Component, Fragment, useEffect, useMemo, useRef, useState, type ErrorInfo, type ReactNode } from 'react';
import type { CleanupEdit, DictationResult, RuntimeState, Settings } from '../shared/types';
import { annotate, annotateRaw, describeEdit, type AnnotatedWord, type EditTone, type ResolvedEdit } from './annotations';

export const api = window.transcriptd;
export const message = (error: unknown) => error instanceof Error ? error.message : String(error);
export const timestamp = (value: string) => { const date = new Date(value); return Number.isNaN(date.valueOf()) ? value : date.toLocaleString(undefined, { dateStyle: 'medium', timeStyle: 'short' }); };
export const clock = (value: string) => { const date = new Date(value); return Number.isNaN(date.valueOf()) ? value : date.toLocaleTimeString(undefined, { hour: 'numeric', minute: '2-digit' }); };
export function dayLabel(value: string): string {
  const date = new Date(value);
  if (Number.isNaN(date.valueOf())) return 'Undated';
  const today = new Date(); today.setHours(0, 0, 0, 0);
  const day = new Date(date); day.setHours(0, 0, 0, 0);
  const diff = Math.round((today.valueOf() - day.valueOf()) / 86_400_000);
  if (diff === 0) return 'Today';
  if (diff === 1) return 'Yesterday';
  return date.toLocaleDateString(undefined, { weekday: 'short', day: 'numeric', month: 'short', year: day.getFullYear() === today.getFullYear() ? undefined : 'numeric' });
}
export function span(ms: number): string {
  const minutes = Math.max(0, Math.round(ms / 60_000));
  if (minutes < 1) return 'under a minute';
  return minutes < 60 ? `${minutes} min` : `${Math.floor(minutes / 60)} h ${minutes % 60} min`;
}
export function elapsed(ms: number): string {
  const s = Math.max(0, Math.floor(ms / 1000));
  const h = Math.floor(s / 3600), m = Math.floor(s / 60) % 60, rest = String(s % 60).padStart(2, '0');
  return h ? `${h}:${String(m).padStart(2, '0')}:${rest}` : `${m}:${rest}`;
}
export function duration(ms: number): string { const seconds = Math.max(0, Math.floor(ms / 1000)); return `${Math.floor(seconds / 60)}:${String(seconds % 60).padStart(2, '0')}`; }

export function useDesktop() {
  const [state, setState] = useState<RuntimeState | null>(null);
  const [settings, setSettings] = useState<Settings | null>(null);
  const [error, setError] = useState('');
  const [notice, setNotice] = useState('');
  useEffect(() => {
    let alive = true, stateEvent = false, settingsEvent = false;
    if (!api) { setError('The desktop bridge is unavailable. Open this page through transcriptd.'); return; }
    const unsubscribe = api.onEvent(event => {
      if (event.type === 'state') { stateEvent = true; setState(event.state); }
      if (event.type === 'settings') { settingsEvent = true; setSettings(event.settings); }
      if (event.type === 'notice') setNotice(event.message);
      if (event.type === 'agent' && event.event.type === 'error') setError(event.event.message);
    });
    api.getState().then(value => { if (alive && !stateEvent) setState(value); }).catch(e => { if (alive) setError(message(e)); });
    api.getSettings().then(value => { if (alive && !settingsEvent) setSettings(value); }).catch(e => { if (alive) setError(message(e)); });
    return () => { alive = false; unsubscribe(); };
  }, []);
  return { state, settings, error, setError, notice, setNotice };
}

export class ErrorBoundary extends Component<{ children: ReactNode }, { error: string }> {
  state = { error: '' };
  static getDerivedStateFromError(error: Error) { return { error: error.message }; }
  componentDidCatch(error: Error, info: ErrorInfo) { console.error('Renderer failed', error, info.componentStack); }
  render() { return this.state.error ? <main className="fatal"><h1>This window stopped responding</h1><p role="alert">{this.state.error}</p><button className="primary" onClick={() => window.location.reload()}>Reload window</button></main> : this.props.children; }
}

export function Feedback({ error, notice, clearError, clearNotice }: { error?: string | null; notice?: string | null; clearError?: () => void; clearNotice?: () => void }) {
  return <>
    {error && <div className="banner banner-error" role="alert"><span>{error}</span>{clearError && <button className="ghost small" onClick={clearError}>Dismiss</button>}</div>}
    {notice && <div className="banner banner-notice" role="status"><span>{notice}</span>{clearNotice && <button className="ghost small" onClick={clearNotice}>Dismiss</button>}</div>}
  </>;
}

/** Renders "ctrl+alt+space" as key caps. */
export function Keys({ combo, fallback = 'Not set' }: { combo?: string | null; fallback?: string }) {
  const parts = (combo ?? '').split('+').map(p => p.trim()).filter(Boolean);
  if (!parts.length) return <span className="muted">{fallback}</span>;
  const name = (p: string) => ({ ctrl: 'Ctrl', control: 'Ctrl', alt: 'Alt', shift: 'Shift', meta: 'Super', super: 'Super', win: 'Win', cmd: 'Cmd', space: 'Space', insert: 'Insert' } as Record<string, string>)[p.toLowerCase()] ?? (p.length === 1 ? p.toUpperCase() : p[0].toUpperCase() + p.slice(1));
  return <span className="keys" aria-label={parts.map(name).join(' plus ')}>{parts.map((p, i) => <kbd key={i} aria-hidden="true">{name(p)}</kbd>)}</span>;
}

type IconName = 'dictations' | 'meetings' | 'vocabulary' | 'settings';
export function Icon({ name }: { name: IconName }) {
  const paths: Record<IconName, ReactNode> = {
    dictations: <><path d="M3 5h10M3 8h7M3 11h9" /><path d="M13.5 9.5v3" /></>,
    meetings: <><path d="M2.5 4.5h7v5h-4l-3 2.5z" /><path d="M11 7h2.5v5l-2-1.5H8" /></>,
    vocabulary: <><path d="M3 12.5 6 3.5h1l3 9" /><path d="M4.2 9.5h4.6" /><path d="M11 12.5h3" /></>,
    settings: <><path d="M3 4.5h10M3 11.5h10" /><circle cx="6" cy="4.5" r="1.6" /><circle cx="10.5" cy="11.5" r="1.6" /></>,
  };
  return <svg className="icon" viewBox="0 0 16 16" aria-hidden="true" focusable="false">{paths[name]}</svg>;
}

/** Waveform settling into a text baseline. */
export function Logo() {
  return <svg className="logo" viewBox="0 0 20 20" aria-hidden="true" focusable="false"><path d="M3 9v2M6 6v8M9 8v4M12 4v12M15 7v6" /><path d="M2 17.5h16" className="logo-line" /></svg>;
}

function toneOf(edits: ResolvedEdit[]): EditTone | '' {
  if (!edits.length) return '';
  return edits.some(e => e.edit.reason === 'vocab') ? 'vocab' : 'edit';
}
const quote = (text: string) => `“${text}”`;
function describeWord(word: AnnotatedWord): string {
  const parts: string[] = [];
  if (word.edits.length) {
    const labels = [...new Set(word.edits.map(e => describeEdit(e.edit).label))].join(', ');
    parts.push(`Heard ${quote(word.heard)}. ${labels}.`);
  }
  if (word.low) parts.push('The model was unsure about this word.');
  return parts.join(' ');
}
function describeMarker(annotation: ResolvedEdit): string {
  const e = annotation.edit;
  const { label } = describeEdit(e);
  return e.cleaned ? `${label}: ${quote(e.raw)} became ${quote(e.cleaned)}.` : `${label}: ${quote(e.raw || 'nothing')}.`;
}

export function EditCounts({ edits, low, empty = 'No words were changed.' }: { edits: CleanupEdit[]; low: number; empty?: string }) {
  const count = (tone: EditTone) => edits.filter(e => describeEdit(e).tone === tone).length;
  const items: Array<[string, number, string]> = [
    ['edit', count('edit'), 'changed'], ['vocab', count('vocab'), 'from vocabulary'], ['removed', count('removed'), 'removed'],
    ['check', low, low === 1 ? 'word to check' : 'words to check'], ['kept', count('kept'), 'kept as spoken'],
  ];
  const shown = items.filter(([, n]) => n > 0);
  if (!shown.length) return <span className="counts-empty">{empty}</span>;
  return <span className="counts">{shown.map(([tone, n, label]) => <span key={tone} className={`count count-${tone}`}><span className="swatch" aria-hidden="true" />{n} {label}</span>)}</span>;
}

export function AnnotatedTranscript({ result, finalText = result.text, onCorrect, onEditingChange, active, onActive, revealRemoved = false, aside }: {
  result: DictationResult; finalText?: string;
  onCorrect?: (input: { heard: string; meant: string; finalText: string }) => Promise<void>;
  onEditingChange?: (editing: boolean) => void;
  /** Edit indices to highlight, shared with the raw view and audit. */
  active?: number[]; onActive?: (edits: number[]) => void;
  revealRemoved?: boolean;
  /** Always shown at the end of the proof line. */
  aside?: ReactNode;
}) {
  const { words, markers } = useMemo(() => annotate(result, finalText), [result, finalText]);
  const [editing, setEditing] = useState<{ word: AnnotatedWord; index: number } | null>(null);
  const [meant, setMeant] = useState('');
  const [pending, setPending] = useState(false);
  const [error, setError] = useState('');
  const [inspect, setInspect] = useState('');
  const input = useRef<HTMLInputElement>(null);
  useEffect(() => { onEditingChange?.(!!editing); return () => onEditingChange?.(false); }, [!!editing, onEditingChange]);
  useEffect(() => { if (editing) input.current?.select(); }, [editing]);
  useEffect(() => { setEditing(null); setError(''); setInspect(''); }, [result]);
  const activeSet = new Set(active ?? []);
  const lowCount = words.filter(w => w.low).length;
  const focus = (text: string, edits: number[]) => { setInspect(text); onActive?.(edits); };
  const blur = () => { setInspect(''); onActive?.([]); };
  const marker = (at: number) => markers.filter(m => m.at === at).map(m => {
    const e = m.annotation.edit;
    const text = describeMarker(m.annotation);
    return <span key={`m${m.annotation.index}`} className={`mark ${e.cleaned ? 'mark-change' : 'mark-removed'} ${activeSet.has(m.annotation.index) ? 'is-active' : ''}`} tabIndex={0} aria-label={text}
      onMouseEnter={() => focus(text, [m.annotation.index])} onMouseLeave={blur} onFocus={() => focus(text, [m.annotation.index])} onBlur={blur}>
      <span className="mark-text" aria-hidden="true"><s>{e.raw}</s>{e.cleaned && <> {e.cleaned}</>}</span>
    </span>;
  });
  async function save() {
    if (!editing || !onCorrect) return;
    setPending(true); setError('');
    try {
      const next = finalText.slice(0, editing.word.start) + meant + finalText.slice(editing.word.end);
      await onCorrect({ heard: editing.word.heard, meant, finalText: next });
      setEditing(null);
    } catch (e) { setError(message(e)); }
    finally { setPending(false); }
  }
  const status = editing ? 'Enter saves, Esc cancels. The raw transcript is not changed.' : inspect;
  return <div className={`annotated ${revealRemoved ? 'reveal-removed' : ''}`}>
    <div className="annotated-text" lang={result.language ?? undefined}>{words.map((word, i) => {
      const tone = toneOf(word.edits);
      const on = word.edits.some(e => activeSet.has(e.index));
      const classes = ['w', tone && `w-${tone}`, word.low ? 'w-check' : '', on ? 'is-active' : ''].filter(Boolean).join(' ');
      const note = describeWord(word);
      const edits = word.edits.map(e => e.index);
      const gap = finalText.slice(i ? words[i - 1].end : 0, word.start);
      const handlers = { onMouseEnter: () => focus(note || (onCorrect ? 'Click to correct this word.' : ''), edits), onMouseLeave: blur, onFocus: () => focus(note || 'Press Enter to correct this word.', edits), onBlur: blur };
      return <Fragment key={`${i}-${word.start}`}>{gap}{marker(i)}{editing?.index === i
        ? <span className="inline-fix" role="group" aria-label={`Correct ${word.text}`}>
            <input ref={input} aria-label="Corrected word or phrase" value={meant} disabled={pending} size={Math.max(4, meant.length + 1)} onChange={e => setMeant(e.target.value)}
              onKeyDown={e => { if (e.key === 'Enter') { e.preventDefault(); void save(); } if (e.key === 'Escape' && !pending) { e.stopPropagation(); setEditing(null); } }} />
            <button className="primary small" disabled={pending || !meant.trim()} onClick={() => void save()}>{pending ? 'Saving…' : 'Save'}</button>
            <button className="ghost small" disabled={pending} onClick={() => setEditing(null)}>Cancel</button>
          </span>
        : onCorrect && word.key
          ? <button className={classes} aria-label={`${word.text}${note ? `. ${note}` : ''} Correct this word.`} {...handlers} onClick={() => { setEditing({ word, index: i }); setMeant(word.text); setError(''); }}>{word.text}</button>
          : note ? <span className={classes} tabIndex={0} aria-label={`${word.text}. ${note}`} {...handlers}>{word.text}</span>
          : <span className={on ? 'w is-active' : undefined}>{word.text}</span>}</Fragment>;
    })}{marker(words.length)}{finalText.slice(words.length ? words[words.length - 1].end : 0)}</div>
    <div className="proofline"><span aria-live="polite">{status ? <span className="inspect">{status}</span> : <EditCounts edits={result.edits} low={lowCount} />}</span>{aside && <span className="proof-aside">{aside}</span>}</div>
    {error && <p role="alert" className="inline-error">{error}</p>}
  </div>;
}

/** The verbatim transcript with every guard decision drawn onto the words it touched. */
export function RawTranscript({ result, active, onActive }: { result: DictationResult; active?: number[]; onActive?: (edits: number[]) => void }) {
  const tokens = useMemo(() => annotateRaw(result), [result]);
  const activeSet = new Set(active ?? []);
  if (!result.raw_text.trim()) return <p className="raw-text muted">Nothing was heard.</p>;
  return <p className="raw-text" lang={result.language ?? undefined}>{tokens.map((token, i) => {
    const gap = result.raw_text.slice(i ? tokens[i - 1].end : 0, token.start);
    const tones = token.edits.map(e => describeEdit(e.edit).tone);
    const tone = tones.includes('removed') ? 'removed' : tones.includes('vocab') ? 'vocab' : tones.includes('edit') ? 'edit' : tones.includes('kept') ? 'kept' : '';
    const on = token.edits.some(e => activeSet.has(e.index));
    const classes = ['r', tone && `r-${tone}`, token.low ? 'r-check' : '', on ? 'is-active' : ''].filter(Boolean).join(' ');
    return <Fragment key={i}>{gap}{classes !== 'r' ? <span className={classes} onMouseEnter={() => onActive?.(token.edits.map(e => e.index))} onMouseLeave={() => onActive?.([])}>{token.text}</span> : token.text}</Fragment>;
  })}</p>;
}

export function CleanupAudit({ result, active, onActive }: { result: DictationResult; active?: number[]; onActive?: (edits: number[]) => void }) {
  const activeSet = new Set(active ?? []);
  if (!result.edits.length) return <p className="muted small">Cleanup proposed no word changes for this dictation.</p>;
  return <div className="table-wrap"><table className="audit">
    <thead><tr><th scope="col">Decision</th><th scope="col">Heard</th><th scope="col">Became</th></tr></thead>
    <tbody>{result.edits.map((edit, i) => {
      const { tone, label } = describeEdit(edit);
      return <tr key={i} className={activeSet.has(i) ? 'is-active' : undefined} tabIndex={0} onMouseEnter={() => onActive?.([i])} onMouseLeave={() => onActive?.([])} onFocus={() => onActive?.([i])} onBlur={() => onActive?.([])}>
        <td><span className={`decision decision-${tone}`}><span className="swatch" aria-hidden="true" />{label}</span></td>
        <td className="evidence">{edit.raw || <span className="muted">nothing</span>}</td>
        <td>{!edit.accepted ? <span className="muted">Kept as spoken{edit.cleaned ? <> (proposed <span className="evidence">{edit.cleaned}</span>)</> : ''}</span> : edit.cleaned || <span className="muted">removed</span>}</td>
      </tr>;
    })}</tbody>
  </table></div>;
}

const SEGMENTS = 16;
export function MicMeter({ db, label = 'Microphone', compact = false }: { db: number; label?: string; compact?: boolean }) {
  const finite = Number.isFinite(db) ? db : -60;
  const value = Math.max(-60, Math.min(0, finite));
  const text = `${finite <= -60 ? 'Below −60' : Math.round(finite)} dBFS`;
  const lit = Math.round((value + 60) / 60 * SEGMENTS);
  const count = compact ? 10 : SEGMENTS;
  const litCount = Math.round(lit / SEGMENTS * count);
  return <span className={`meter ${compact ? 'meter-compact' : ''}`} role="meter" aria-label={`${label} level`} aria-valuemin={-60} aria-valuemax={0} aria-valuenow={value} aria-valuetext={text} title={`${label}: ${text}`}>
    {Array.from({ length: count }, (_, i) => <span key={i} className={`seg ${i < litCount ? 'on' : ''} ${i >= count - 2 ? 'hot' : ''}`} />)}
  </span>;
}
