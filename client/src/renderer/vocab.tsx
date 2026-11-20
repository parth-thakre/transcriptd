import { useEffect, useRef, useState } from 'react';
import { createRoot } from 'react-dom/client';
import type { VocabSuggestion } from '../shared/types';
import { ErrorBoundary, Feedback, api, message } from './components';
import './styles.css';

type Action = 'add' | 'replace' | 'skip' | 'never';

function VocabPrompt() {
  const [suggestion, setSuggestion] = useState<VocabSuggestion | null>(null);
  const [loading, setLoading] = useState(true);
  const [pending, setPending] = useState(false);
  const [error, setError] = useState('');
  const addButton = useRef<HTMLButtonElement>(null);
  useEffect(() => {
    let alive = true, received = false;
    if (!api) { setError('The desktop bridge is unavailable. Open transcriptd to use vocabulary suggestions.'); setLoading(false); return; }
    const unsubscribe = api.onEvent(event => { if (event.type === 'suggestion') { received = true; setSuggestion(event.suggestion); setLoading(false); setError(''); } });
    api.getSuggestion().then(value => { if (alive && !received) setSuggestion(value); }).catch(e => { if (alive) setError(message(e)); }).finally(() => { if (alive) setLoading(false); });
    return () => { alive = false; unsubscribe(); };
  }, []);
  // A new suggestion always starts with focus on the default action.
  useEffect(() => { if (suggestion) addButton.current?.focus(); }, [suggestion]);
  async function resolve(action: Action) {
    if (pending) return;
    setPending(true); setError('');
    try { await api.resolveSuggestion(action); }
    catch (e) { setError(message(e)); }
    finally { setPending(false); }
  }
  useEffect(() => {
    if (!suggestion) return;
    const onKey = (event: KeyboardEvent) => {
      if (event.key === 'Escape') { event.preventDefault(); void resolve('skip'); return; }
      // Enter on another focused button activates that button natively.
      const target = event.target as HTMLElement | null;
      if (event.key === 'Enter' && !event.repeat && (!target || target === document.body || target === addButton.current)) { event.preventDefault(); void resolve('add'); }
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  });
  return <main className="prompt" aria-busy={loading || pending}>
    {loading ? <p className="muted" role="status">Loading suggestion…</p> : suggestion ? <>
      <div className="prompt-body">
        <h1 className="prompt-title">Add <span className="prompt-term" lang="und">{suggestion.term}</span> to your vocabulary?</h1>
        <p className="prompt-heard">Heard as <q className="evidence">{suggestion.heard_as}</q>.</p>
        <p className="prompt-help" id="replace-help">Always replace also rewrites <q className="evidence">{suggestion.heard_as}</q> in future dictations, and marks it in green.</p>
      </div>
      <Feedback error={error} />
      <div className="prompt-actions">
        <button ref={addButton} className="primary" disabled={pending} onClick={() => void resolve('add')}>Add<kbd className="key-hint" aria-hidden="true">Enter</kbd></button>
        <button disabled={pending} aria-describedby="replace-help" onClick={() => void resolve('replace')}>Add and always replace</button>
      </div>
      <div className="prompt-secondary">
        <button className="ghost" disabled={pending} onClick={() => void resolve('skip')}>Not now<kbd className="key-hint" aria-hidden="true">Esc</kbd></button>
        <button className="ghost" disabled={pending} onClick={() => void resolve('never')}>Never for this word</button>
      </div>
    </> : <>
      <Feedback error={error} />
      <p className="muted">No vocabulary suggestion is waiting. Correct a word in the overlay and transcriptd will offer to remember it.</p>
    </>}
  </main>;
}
createRoot(document.getElementById('root')!).render(<ErrorBoundary><VocabPrompt /></ErrorBoundary>);
