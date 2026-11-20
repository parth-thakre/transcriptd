import { useCallback, useEffect, useRef, useState } from 'react';
import { createRoot } from 'react-dom/client';
import type { DictationResult } from '../shared/types';
import { AnnotatedTranscript, ErrorBoundary, Feedback, Keys, MicMeter, api, message, useDesktop } from './components';
import './styles.css';

function Overlay() {
  const { state, settings, error, setError, notice, setNotice } = useDesktop();
  const [recording, setRecording] = useState(false);
  const [processing, setProcessing] = useState(false);
  const [db, setDb] = useState(-60);
  const [partial, setPartial] = useState('');
  const [result, setResult] = useState<DictationResult | null>(null);
  const [finalText, setFinalText] = useState('');
  const [hover, setHover] = useState(false), [focus, setFocus] = useState(false), [editing, setEditing] = useState(false);
  const [injected, setInjected] = useState<boolean | null>(null);
  const shell = useRef<HTMLElement | null>(null);
  const lastSnapshot = useRef('');
  const awaitingFinal = useRef(false);
  const generation = useRef(0);
  const resultIdentity = useRef('');
  const editChange = useCallback((value: boolean) => setEditing(value), []);
  useEffect(() => { document.documentElement.classList.add('overlay-doc'); return () => document.documentElement.classList.remove('overlay-doc'); }, []);
  useEffect(() => {
    if (!state) return;
    setRecording(state.recording);
    const evidence = state.lastResult ? JSON.stringify([state.lastResult.dictation_id, state.lastResult.raw_text]) : '';
    if (evidence !== resultIdentity.current) { generation.current++; resultIdentity.current = evidence; }
    const identity = state.lastResult ? JSON.stringify([state.lastResult.dictation_id, state.lastResult.raw_text, state.lastResult.text]) : '';
    if (!state.lastResult) { setResult(null); setFinalText(''); }
    else if (identity !== lastSnapshot.current && !state.recording && !awaitingFinal.current) { setResult(state.lastResult); setFinalText(state.lastResult.text); }
    lastSnapshot.current = identity;
  }, [state?.recording, state?.lastResult]);
  useEffect(() => api?.onEvent(event => {
    if (event.type !== 'agent') return;
    const e = event.event;
    if (e.type === 'ptt.down') { generation.current++; awaitingFinal.current = true; setRecording(true); setProcessing(false); setPartial(''); setResult(null); setFinalText(''); setDb(-60); setInjected(null); setError(''); setNotice(''); }
    if (e.type === 'ptt.up') { awaitingFinal.current = true; setRecording(false); setProcessing(true); setDb(-60); }
    if (e.type === 'level') setDb(e.db);
    if (e.type === 'partial') setPartial(e.text);
    if (e.type === 'final') { generation.current++; resultIdentity.current = JSON.stringify([e.result.dictation_id, e.result.raw_text]); awaitingFinal.current = false; setRecording(false); setProcessing(false); setResult(e.result); setFinalText(e.result.text); setInjected(e.injected); }
    if (e.type === 'error') { awaitingFinal.current = false; setProcessing(false); }
  }), []);
  useEffect(() => {
    if (!api) return;
    api.overlayHold(hover || focus || editing).catch(e => setError(message(e)));
  }, [hover, focus, editing]);
  useEffect(() => () => { generation.current++; void api?.overlayHold(false).catch(() => {}); }, []);
  useEffect(() => { if (!editing) setFocus(!!shell.current?.contains(document.activeElement)); }, [editing]);
  useEffect(() => { if (state?.agentStatus === 'offline' && (processing || recording)) { awaitingFinal.current = false; setProcessing(false); setRecording(false); setError('The agent disconnected. Open settings to restart it.'); } }, [state?.agentStatus, processing, recording]);
  const phase = recording ? 'recording' : processing ? 'processing' : result ? 'result' : 'idle';
  const hotkeyHint = settings?.hotkeyMode === 'toggle' ? 'Press' : 'Release';
  const footer = <>
    <span className={`paste-state ${injected === false ? 'is-off' : ''}`}>{injected === true ? 'Pasted' : injected === false ? 'Not pasted' : 'Last dictation'}</span>
    <button className="ghost small" onClick={() => api.openMain('dictations').catch(e => setError(message(e)))}>Review</button>
  </>;
  return <main ref={shell} className={`pill pill-${phase}`} data-phase={phase} onMouseEnter={() => setHover(true)} onMouseLeave={() => setHover(false)} onFocusCapture={() => setFocus(true)} onBlurCapture={e => { if (!e.currentTarget.contains(e.relatedTarget as Node | null)) setFocus(false); }}>
    {phase === 'recording' && <div className="pill-row">
      <span className="rec-dot" aria-hidden="true" />
      <MicMeter db={db} compact />
      <p className="pill-live" aria-live="polite">{settings?.showPartials ? <span className="pill-live-text">{partial || <span className="muted">Listening…</span>}</span> : <span className="muted">Listening…</span>}</p>
      <span className="pill-hint">{hotkeyHint} <Keys combo={settings?.hotkey} fallback="your hotkey" /></span>
    </div>}
    {phase === 'processing' && <div className="pill-row" role="status"><span className="busy-dot" aria-hidden="true" /><p className="pill-live"><span className="muted">Transcribing…</span></p></div>}
    {phase === 'idle' && <div className="pill-row"><span className="idle-dot" aria-hidden="true" /><p className="pill-live"><span className="muted">{settings?.hotkeyMode === 'toggle' ? 'Press' : 'Hold'} <Keys combo={settings?.hotkey} fallback="your dictation hotkey" /> to dictate</span></p></div>}
    {phase === 'result' && result && <div className="pill-result">
      {finalText ? <AnnotatedTranscript result={result} finalText={finalText} onEditingChange={editChange} aside={footer} onCorrect={async input => {
        const started = generation.current;
        await api.correct({ ...input, dictation_id: result.dictation_id, source: 'overlay' });
        if (generation.current !== started) return;
        setFinalText(input.finalText);
        setNotice('Correction saved. Text already pasted elsewhere is unchanged.');
      }} /> : <div className="proofline"><span className="muted">No speech was detected.</span><span className="proof-aside">{footer}</span></div>}
    </div>}
    {(error || notice) && <div className="pill-feedback"><Feedback error={error} notice={notice} clearError={() => setError('')} clearNotice={() => setNotice('')} /></div>}
  </main>;
}

createRoot(document.getElementById('root')!).render(<ErrorBoundary><Overlay /></ErrorBoundary>);
