import { useCallback, useEffect, useRef, useState, type KeyboardEvent as ReactKeyboardEvent } from 'react';
import { createRoot } from 'react-dom/client';
import type { DictationDetail, DictationRow, Meeting, Page, RuntimeState, Segment, Settings } from '../shared/types';
import { AnnotatedTranscript, CleanupAudit, ErrorBoundary, Feedback, Icon, Keys, Logo, MicMeter, RawTranscript, api, clock, dayLabel, duration, elapsed, message, span, timestamp, useDesktop } from './components';
import { SettingsPanel, VocabularyPanel } from './panels';
import './styles.css';

const pages: Array<{ id: Page; label: string }> = [
  { id: 'dictations', label: 'Dictations' }, { id: 'meetings', label: 'Meetings' },
  { id: 'vocabulary', label: 'Vocabulary' }, { id: 'settings', label: 'Settings' },
];

/** Arrow keys move the selection inside a history list. */
function listKeys<T extends { id: number }>(rows: T[], selected: number | null, select: (id: number) => void) {
  return (event: ReactKeyboardEvent<HTMLElement>) => {
    if (event.key !== 'ArrowDown' && event.key !== 'ArrowUp') return;
    event.preventDefault();
    const at = rows.findIndex(row => row.id === selected);
    const next = rows[Math.max(0, Math.min(rows.length - 1, at + (event.key === 'ArrowDown' ? 1 : -1)))];
    if (!next) return;
    select(next.id);
    (event.currentTarget.querySelector(`[data-id="${next.id}"]`) as HTMLElement | null)?.focus();
  };
}
function groupByDay<T>(rows: T[], date: (row: T) => string): Array<{ day: string; rows: T[] }> {
  const groups: Array<{ day: string; rows: T[] }> = [];
  for (const row of rows) {
    const day = dayLabel(date(row));
    const last = groups[groups.length - 1];
    if (last?.day === day) last.rows.push(row); else groups.push({ day, rows: [row] });
  }
  return groups;
}

function Dictations({ settings }: { settings: Settings | null }) {
  const [query, setQuery] = useState(''), [rows, setRows] = useState<DictationRow[]>([]);
  const [selected, setSelected] = useState<number | null>(null), [detail, setDetail] = useState<DictationDetail | null>(null);
  const [revision, setRevision] = useState(0), [loading, setLoading] = useState(true), [detailLoading, setDetailLoading] = useState(false);
  const [error, setError] = useState(''), [notice, setNotice] = useState(''), [injecting, setInjecting] = useState(false);
  const [active, setActive] = useState<number[]>([]), [reveal, setReveal] = useState(false);
  const lastFinal = useRef('');
  const finalCache = useRef(new Map<number, string>());
  const finalText = (selected == null ? undefined : finalCache.current.get(selected)) ?? detail?.result.text ?? '';
  useEffect(() => {
    let alive = true;
    setLoading(true);
    const timer = setTimeout(() => {
      api.server.dictations(query.trim() || undefined).then(value => { if (alive) { for (const row of value) finalCache.current.set(row.id, row.text); setRows(value); setSelected(current => value.some(row => row.id === current) ? current : value[0]?.id ?? null); } }).catch(e => { if (alive) setError(message(e)); }).finally(() => { if (alive) setLoading(false); });
    }, 200);
    return () => { alive = false; clearTimeout(timer); };
  }, [query, revision]);
  useEffect(() => api.onEvent(event => {
    if (event.type === 'agent' && event.event.type === 'final') { setRevision(n => n + 1); if (event.event.result.dictation_id != null) setSelected(event.event.result.dictation_id); }
    if (event.type === 'state' && event.state.lastResult) { if (event.state.lastResult.dictation_id != null) finalCache.current.set(event.state.lastResult.dictation_id, event.state.lastResult.text); const identity = `${event.state.lastResult.dictation_id}:${event.state.lastResult.text}`; if (identity !== lastFinal.current) { lastFinal.current = identity; setRevision(n => n + 1); } }
  }), []);
  useEffect(() => {
    if (selected === null) { setDetail(null); setDetailLoading(false); return; }
    let alive = true;
    setDetailLoading(true); setDetail(null); setActive([]);
    api.server.dictation(selected).then(value => { if (alive) setDetail(value); }).catch(e => { if (alive) setError(message(e)); }).finally(() => { if (alive) setDetailLoading(false); });
    return () => { alive = false; };
  }, [selected, revision]);
  async function inject() {
    if (!detail) return;
    setInjecting(true); setError(''); setNotice('Switch to the target app now. Pasting starts in 1.5 seconds.');
    try { await api.inject(finalText); }
    catch (e) { setError(message(e)); }
    finally { setInjecting(false); }
  }
  const row = rows.find(r => r.id === selected);
  return <section className="page page-split" aria-labelledby="dictations-title">
    <header className="page-head">
      <h1 id="dictations-title">Dictations</h1>
      <div className="page-tools"><button className="ghost" onClick={() => setRevision(n => n + 1)} disabled={loading}>Refresh</button></div>
    </header>
    <Feedback error={error} notice={notice} clearError={() => setError('')} clearNotice={() => setNotice('')} />
    <div className="split">
      <aside className="list-pane" aria-label="Dictation history">
        <label className="search"><span className="sr-only">Search dictations</span><input type="search" placeholder="Search what you said" value={query} onChange={e => setQuery(e.target.value)} /></label>
        <div className="list-scroll" aria-busy={loading} onKeyDown={listKeys(rows, selected, setSelected)}>
          {loading && !rows.length && <p className="list-note" role="status">Loading history…</p>}
          {!loading && !rows.length && <p className="list-note">{query ? 'Nothing you said matches this search.' : <>Dictate with <Keys combo={settings?.hotkey} fallback="your hotkey" /> and it will appear here.</>}</p>}
          {groupByDay(rows, r => r.created_at).map(group => <div className="day" key={group.day}>
            <h2 className="day-label">{group.day}</h2>
            <ul className="rows">{group.rows.map(item => <li key={item.id}>
              <button data-id={item.id} className={`row ${selected === item.id ? 'is-selected' : ''}`} aria-current={selected === item.id ? 'true' : undefined} tabIndex={selected === item.id || (selected == null && item === rows[0]) ? 0 : -1} onClick={() => setSelected(item.id)}>
                <span className="row-meta"><time dateTime={item.created_at}>{clock(item.created_at)}</time><span>{duration(item.duration_ms)}</span>{item.app_hint && <span className="row-app">{item.app_hint}</span>}</span>
                <span className="row-text">{item.text || item.raw_text || <span className="muted">No speech detected</span>}</span>
              </button>
            </li>)}</ul>
          </div>)}
        </div>
      </aside>
      <article className="detail-pane" aria-busy={detailLoading} aria-label="Selected dictation">
        {detailLoading ? <p className="detail-note" role="status">Loading dictation…</p> : detail ? <div className="doc">
          <div className="doc-head">
            <p className="doc-meta"><time dateTime={detail.created_at}>{timestamp(detail.created_at)}</time>{row && <span>{duration(row.duration_ms)}</span>}<span>{detail.result.profile ?? row?.profile}</span>{(detail.result.language ?? row?.language) && <span className="lang">{detail.result.language ?? row?.language}</span>}{row?.app_hint && <span>{row.app_hint}</span>}</p>
            <div className="doc-actions">
              <button className="primary" disabled={!finalText || injecting} aria-describedby="inject-help" onClick={() => void inject()}>{injecting ? 'Preparing to paste…' : 'Paste again'}</button>
              <p className="doc-help" id="inject-help">Hides this window, then pastes after 1.5 s into the app you focus.</p>
            </div>
          </div>
          <section className="proof" aria-labelledby="final-heading">
            <div className="proof-head"><h2 id="final-heading">Final text</h2>
              <label className="switch"><input type="checkbox" checked={reveal} onChange={e => setReveal(e.target.checked)} />Show removed words</label></div>
            <div className="final-text"><AnnotatedTranscript result={detail.result} finalText={finalText} active={active} onActive={setActive} revealRemoved={reveal} /></div>
          </section>
          <section className="evidence-block" aria-labelledby="raw-heading">
            <h2 id="raw-heading">Heard, word for word</h2>
            <RawTranscript result={detail.result} active={active} onActive={setActive} />
          </section>
          <section aria-labelledby="audit-heading">
            <h2 id="audit-heading">Cleanup decisions</h2>
            <CleanupAudit result={detail.result} active={active} onActive={setActive} />
          </section>
        </div> : <div className="detail-empty"><h2>Nothing selected</h2><p>Pick a dictation to see what the model heard next to what was pasted, with every cleanup edit marked.</p></div>}
      </article>
    </div>
  </section>;
}

function segmentIdentity(segment: Segment): string { return segment.id != null ? `id:${segment.id}` : `${segment.channel}|${segment.speaker ?? ''}|${segment.start_ms}|${segment.end_ms}|${segment.text}`; }
function mergeSegments(stored: Segment[], incoming: Segment[]): Segment[] {
  const result = new Map<string, Segment>();
  const contentKey = (segment: Segment) => `${segment.channel}|${segment.start_ms}|${segment.end_ms}|${segment.text}`;
  const storedContent = new Set(stored.map(contentKey));
  for (const segment of stored) result.set(segmentIdentity(segment), segment);
  for (const segment of incoming) { if (!storedContent.has(contentKey(segment)) && !result.has(segmentIdentity(segment))) result.set(segmentIdentity(segment), segment); }
  return [...result.values()].sort((a, b) => a.start_ms - b.start_ms || a.end_ms - b.end_ms);
}
const isMe = (segment: Segment) => segment.channel === 'me' || segment.channel === 'mic';
function speakerOf(segment: Segment): string {
  return segment.speaker || (isMe(segment) ? 'You' : segment.channel === 'them' || segment.channel === 'loopback' ? 'System audio' : segment.channel || 'Speaker');
}
function TranscriptSegments({ segments }: { segments: Segment[] }) {
  const groups: Array<{ speaker: string; me: boolean; segments: Segment[] }> = [];
  for (const segment of segments) {
    const speaker = speakerOf(segment);
    const previous = groups[groups.length - 1];
    if (previous?.speaker === speaker) previous.segments.push(segment); else groups.push({ speaker, me: isMe(segment), segments: [segment] });
  }
  const speakers = [...new Set(groups.filter(g => !g.me).map(g => g.speaker))];
  return <div className="script" aria-label="Meeting transcript" aria-live="polite" aria-relevant="additions text">{groups.length ? groups.map((group, i) =>
    <section className={`turn ${group.me ? 'turn-me' : `turn-other turn-hue-${speakers.indexOf(group.speaker) % 4}`}`} key={`${segmentIdentity(group.segments[0])}-${i}`}>
      <h3 className="turn-speaker">{group.speaker}<time>{duration(group.segments[0].start_ms)}</time></h3>
      <div className="turn-body">{group.segments.map(segment => <p className={`seg-text ${segment.live ? 'is-live' : ''}`} key={segmentIdentity(segment)} title={`${duration(segment.start_ms)} to ${duration(segment.end_ms)}`}>{segment.text}{segment.live && <span className="live-tag">live</span>}</p>)}</div>
    </section>) : <p className="detail-note">No transcript yet. Live segments appear here while recording. Process stored audio to produce the final transcript.</p>}</div>;
}

function MeetingDetail({ id, active, live, clearLive, onChange, onDelete }: { id: number; active: boolean; live: Segment[]; clearLive: (id: number) => void; onChange: () => void; onDelete: () => void }) {
  const [meeting, setMeeting] = useState<Meeting | null>(null);
  const [title, setTitle] = useState(''), [notes, setNotes] = useState(''), [dirty, setDirty] = useState(false);
  const [error, setError] = useState(''), [notice, setNotice] = useState(''), [busy, setBusy] = useState('');
  const [confirmDelete, setConfirmDelete] = useState(false), [loading, setLoading] = useState(true);
  const mounted = useRef(true), draftDirty = useRef(false), request = useRef(0);
  const load = useCallback(async () => {
    const version = ++request.current;
    try {
      const value = await api.server.meeting(id);
      if (!mounted.current || version !== request.current) return;
      setMeeting(value);
      if (value.segments?.some(segment => segment.final)) clearLive(id);
      if (!draftDirty.current) { setTitle(value.title); setNotes(value.notes); }
    } catch (e) { if (mounted.current) setError(message(e)); }
    finally { if (mounted.current) setLoading(false); }
  }, [id, clearLive]);
  useEffect(() => { mounted.current = true; void load(); return () => { mounted.current = false; }; }, [load]);
  useEffect(() => api.onEvent(event => {
    if (event.type !== 'agent') return;
    const e = event.event;
    if (e.type === 'meeting.stopped' && (e.meeting_id === id || e.meeting_id == null && active)) { void load(); onChange(); }
  }), [id, active, load, onChange]);
  const act = async (name: string, task: () => Promise<void>) => {
    setBusy(name); setError(''); setNotice('');
    try { await task(); } catch (e) { if (mounted.current) setError(message(e)); }
    finally { if (mounted.current) setBusy(''); }
  };
  function markDirty() { draftDirty.current = true; setDirty(true); }
  const segments = mergeSegments(meeting?.segments ?? [], live);
  const formId = `meeting-form-${id}`;
  if (loading) return <p className="detail-note" role="status">Loading meeting…</p>;
  return <>{meeting ? <div className="doc">
    <div className="doc-head">
      <div className="doc-title">
        <input className="title-input" form={formId} aria-label="Meeting title" value={title} placeholder="Untitled meeting" onChange={e => { setTitle(e.target.value); markDirty(); }} disabled={busy === 'notes'} />
        <p className="doc-meta"><time dateTime={meeting.started_at}>{timestamp(meeting.started_at)}</time>{meeting.ended_at && <span>{span(new Date(meeting.ended_at).valueOf() - new Date(meeting.started_at).valueOf())}</span>}{active ? <span className="rec-text">Recording</span> : meeting.status !== 'done' && <span>{meeting.status}</span>}<span>{meeting.profile}</span></p>
      </div>
    </div>
    <div className="toolbar">
      <button disabled={!!busy || active || !meeting.has_audio} title={meeting.has_audio ? undefined : 'Available after a recording has been stored'} onClick={() => void act('process', async () => { await api.server.processMeeting(id); await load(); onChange(); setNotice('Processing requested. Refresh to check progress.'); })}>{busy === 'process' ? 'Processing…' : 'Process audio'}</button>
      <button disabled={!!busy || active || !segments.length} onClick={() => void act('summary', async () => { const result = await api.server.summarize(id); setMeeting(current => current && { ...current, summary: result.summary }); setNotice('Summary updated.'); onChange(); })}>{busy === 'summary' ? 'Summarizing…' : meeting.summary ? 'Summarize again' : 'Summarize'}</button>
      <button disabled={!!busy} onClick={() => void act('export', async () => { const text = await api.server.transcript(id, 'md'); const saved = await api.saveText(`${meeting.title.replace(/[^\p{L}\p{N} _-]/gu, '').slice(0, 80) || 'meeting'}.md`, text); if (saved) setNotice('Markdown transcript saved.'); })}>Export Markdown</button>
      <button className="ghost" disabled={!!busy} onClick={() => void act('refresh', load)}>Refresh</button>
      <span className="toolbar-gap" />
      <button className="ghost danger" disabled={!!busy || active} onClick={() => setConfirmDelete(true)}>Delete</button>
    </div>
    <Feedback error={error} notice={notice} clearError={() => setError('')} clearNotice={() => setNotice('')} />
    {!meeting.has_audio && !active && <p className="muted small">Audio processing becomes available once a recording is stored.</p>}
    {confirmDelete && <div className="confirm" role="alert"><p>Delete this meeting and its transcript? This cannot be undone.</p><div className="actions"><button className="danger-solid" disabled={!!busy} onClick={() => void act('delete', async () => { await api.server.deleteMeeting(id); onDelete(); })}>Delete meeting</button><button disabled={!!busy} onClick={() => setConfirmDelete(false)}>Keep meeting</button></div></div>}
    {meeting.summary && <section className="summary" aria-labelledby="summary-heading"><h2 id="summary-heading">Summary</h2><div className="summary-text">{meeting.summary}</div></section>}
    <form id={formId} className="notes" onSubmit={event => { event.preventDefault(); void act('notes', async () => { const value = await api.server.patchMeeting(id, { title: title.trim() || 'Untitled meeting', notes }); setMeeting(current => ({ ...value, segments: value.segments ?? current?.segments })); setTitle(value.title); setNotes(value.notes); draftDirty.current = false; setDirty(false); setNotice('Meeting notes saved.'); onChange(); }); }}>
      <label className="notes-label" htmlFor={`${formId}-notes`}>Your notes</label>
      <textarea id={`${formId}-notes`} rows={3} value={notes} onChange={e => { setNotes(e.target.value); markDirty(); }} placeholder="Decisions, open questions, follow-ups" disabled={busy === 'notes'} />
      {dirty && <div className="actions"><button disabled={!!busy} className="primary small" type="submit">{busy === 'notes' ? 'Saving…' : 'Save title and notes'}</button><span className="muted small">Unsaved changes</span></div>}
    </form>
    <section aria-labelledby="transcript-heading"><div className="section-head"><h2 id="transcript-heading">Transcript</h2>{active && <span className="rec-text small">Updating live</span>}</div><TranscriptSegments segments={segments} /></section>
  </div> : <div className="detail-empty"><Feedback error={error} /><p>The meeting could not be loaded.</p><button onClick={() => void load()}>Try again</button></div>}</>;
}

function RecordingBar({ meeting, onStop, stopping, onOpen }: { meeting: Meeting | undefined; onStop: () => void; stopping: boolean; onOpen: () => void }) {
  const [now, setNow] = useState(Date.now());
  const [levels, setLevels] = useState({ me: -60, them: -60 });
  useEffect(() => { const timer = setInterval(() => setNow(Date.now()), 1000); return () => clearInterval(timer); }, []);
  useEffect(() => api.onEvent(event => { if (event.type === 'agent' && event.event.type === 'meeting.level') setLevels({ me: event.event.me_db, them: event.event.them_db }); }), []);
  const started = meeting ? new Date(meeting.started_at).valueOf() : NaN;
  return <div className="recbar" role="status">
    <span className="rec-dot" aria-hidden="true" />
    <button className="recbar-title" onClick={onOpen}>{meeting?.title || 'Meeting'}</button>
    {Number.isFinite(started) && <time className="recbar-time">{elapsed(now - started)}</time>}
    <span className="recbar-level">You <MicMeter db={levels.me} label="Your microphone" compact /></span>
    <span className="recbar-level">Others <MicMeter db={levels.them} label="System audio" compact /></span>
    <button className="danger-solid small" disabled={stopping} onClick={onStop}>{stopping ? 'Stopping…' : 'Stop recording'}</button>
  </div>;
}

function Meetings({ state, settings, liveSegments, clearLive }: { state: RuntimeState | null; settings: Settings | null; liveSegments: Record<number, Segment[]>; clearLive: (id: number) => void }) {
  const [rows, setRows] = useState<Meeting[]>([]), [query, setQuery] = useState(''), [title, setTitle] = useState('');
  const [selected, setSelected] = useState<number | null>(null), [revision, setRevision] = useState(0);
  const [error, setError] = useState(''), [busy, setBusy] = useState(''), [loading, setLoading] = useState(true);
  const refresh = useCallback(() => setRevision(n => n + 1), []);
  useEffect(() => {
    let alive = true; setLoading(true);
    const timer = setTimeout(() => { api.server.meetings(query.trim() || undefined).then(value => { if (alive) { setRows(value); setSelected(current => value.some(row => row.id === current) ? current : value.find(row => row.id === state?.activeMeetingId)?.id ?? value[0]?.id ?? null); } }).catch(e => { if (alive) setError(message(e)); }).finally(() => { if (alive) setLoading(false); }); }, 200);
    return () => { alive = false; clearTimeout(timer); };
  }, [query, revision, state?.activeMeetingId]);
  useEffect(() => api.onEvent(event => { if (event.type === 'agent' && (event.event.type === 'meeting.started' || event.event.type === 'meeting.stopped')) refresh(); }), [refresh]);
  async function start(record: boolean) {
    setBusy(record ? 'start' : 'create'); setError('');
    try { const meeting = record ? await api.startMeeting(title.trim() || undefined) : await api.server.createMeeting({ title: title.trim() || 'Untitled meeting', profile: settings?.profile }); setTitle(''); setQuery(''); setSelected(meeting.id); refresh(); }
    catch (e) { setError(message(e)); } finally { setBusy(''); }
  }
  async function stop() { setBusy('stop'); setError(''); try { await api.stopMeeting(); refresh(); } catch (e) { setError(message(e)); } finally { setBusy(''); } }
  const activeId = state?.activeMeetingId ?? null;
  const cannotRecord = activeId != null || !!state?.recording || state?.agentStatus !== 'online';
  return <section className="page page-split" aria-labelledby="meetings-title">
    <header className="page-head">
      <h1 id="meetings-title">Meetings</h1>
      <form className="page-tools new-meeting" onSubmit={e => { e.preventDefault(); void start(true); }}>
        <label><span className="sr-only">New meeting title</span><input placeholder="Title for a new meeting" value={title} onChange={e => setTitle(e.target.value)} /></label>
        <button className="primary" type="submit" disabled={!!busy || cannotRecord} title={state?.agentStatus !== 'online' ? 'The local agent must be online to record' : undefined}>{busy === 'start' ? 'Starting…' : 'Start recording'}</button>
        <button type="button" disabled={!!busy} onClick={() => void start(false)}>{busy === 'create' ? 'Creating…' : 'Notes only'}</button>
      </form>
    </header>
    {activeId != null && <RecordingBar meeting={rows.find(r => r.id === activeId)} stopping={busy === 'stop'} onStop={() => void stop()} onOpen={() => setSelected(activeId)} />}
    <Feedback error={error} clearError={() => setError('')} />
    <div className="split">
      <aside className="list-pane" aria-label="Meeting history">
        <label className="search"><span className="sr-only">Search meetings</span><input type="search" placeholder="Search meetings" value={query} onChange={e => setQuery(e.target.value)} /></label>
        <div className="list-scroll" aria-busy={loading} onKeyDown={listKeys(rows, selected, setSelected)}>
          {loading && !rows.length && <p className="list-note" role="status">Loading meetings…</p>}
          {!loading && !rows.length && <p className="list-note">{query ? 'No meetings match this search.' : 'Start a recording to capture your microphone and the other side of the call.'}</p>}
          {groupByDay(rows, r => r.started_at).map(group => <div className="day" key={group.day}>
            <h2 className="day-label">{group.day}</h2>
            <ul className="rows">{group.rows.map(meeting => <li key={meeting.id}>
              <button data-id={meeting.id} className={`row ${selected === meeting.id ? 'is-selected' : ''}`} aria-current={selected === meeting.id ? 'true' : undefined} tabIndex={selected === meeting.id || (selected == null && meeting === rows[0]) ? 0 : -1} onClick={() => setSelected(meeting.id)}>
                <span className="row-title">{meeting.title || 'Untitled meeting'}</span>
                <span className="row-meta"><time dateTime={meeting.started_at}>{clock(meeting.started_at)}</time>{meeting.ended_at && <span>{span(new Date(meeting.ended_at).valueOf() - new Date(meeting.started_at).valueOf())}</span>}{meeting.id === activeId ? <span className="rec-text">Recording</span> : meeting.status !== 'done' && <span>{meeting.status}</span>}</span>
              </button>
            </li>)}</ul>
          </div>)}
        </div>
      </aside>
      <article className="detail-pane" aria-label="Selected meeting">{selected != null ? <MeetingDetail key={selected} id={selected} active={activeId === selected} live={liveSegments[selected] ?? []} clearLive={clearLive} onChange={refresh} onDelete={() => { setSelected(null); refresh(); }} /> : <div className="detail-empty"><h2>No meeting selected</h2><p>Start a recording, or pick a meeting to read its transcript and add notes.</p></div>}</article>
    </div>
  </section>;
}

function Main() {
  const { state, settings, error, setError, notice, setNotice } = useDesktop();
  const connectionKey = settings ? JSON.stringify([settings.serverUrl, settings.token]) : 'loading';
  const [liveSegments, setLiveSegments] = useState<Record<number, Segment[]>>({});
  useEffect(() => { setLiveSegments({}); }, [connectionKey]);
  const activeMeeting = useRef<number | null>(null);
  activeMeeting.current = state?.activeMeetingId ?? null;
  const clearLive = useCallback((id: number) => setLiveSegments(current => { const next = { ...current }; delete next[id]; return next; }), []);
  useEffect(() => api?.onEvent(event => {
    if (event.type === 'state') activeMeeting.current = event.state.activeMeetingId;
    if (event.type !== 'agent') return;
    const e = event.event;
    if (e.type === 'meeting.segment') { const id = e.meeting_id ?? activeMeeting.current; if (id != null) setLiveSegments(current => ({ ...current, [id]: mergeSegments(current[id] ?? [], [{ ...e.segment, live: true }]) })); }
  }), []);
  const [page, setPage] = useState<Page>(() => { const initial = new URLSearchParams(window.location.search).get('page'); return pages.some(p => p.id === initial) ? initial as Page : 'dictations'; });
  const initialPageChosen = useRef(!!new URLSearchParams(window.location.search).get('page'));
  useEffect(() => { if (settings && !initialPageChosen.current) { initialPageChosen.current = true; if (!settings.token) setPage('settings'); } }, [settings]);
  useEffect(() => api?.onEvent(event => { if (event.type === 'navigate') { initialPageChosen.current = true; setPage(event.page); } }), []);
  if (!api) return <main className="fatal"><h1>Open transcriptd to continue</h1><Feedback error={error || 'The desktop bridge is unavailable.'} /></main>;
  const status = state?.agentStatus ?? 'starting';
  return <div className="app">
    <a className="skip-link" href="#main-content">Skip to content</a>
    <aside className="side">
      <div className="brand"><Logo /><span>transcriptd</span></div>
      <nav aria-label="Main navigation">{pages.map(item => <button key={item.id} className={`nav-item ${page === item.id ? 'is-current' : ''}`} aria-current={page === item.id ? 'page' : undefined} onClick={() => setPage(item.id)}><Icon name={item.id} />{item.label}{item.id === 'meetings' && state?.activeMeetingId != null && <span className="rec-dot small" aria-label="recording" />}</button>)}</nav>
      <div className="side-foot">
        {state?.recording && <p className="rec-text"><span className="rec-dot small" aria-hidden="true" /> Dictating</p>}
        <p className="side-hotkey">{settings?.hotkeyMode === 'toggle' ? 'Press to dictate' : 'Hold to dictate'}<Keys combo={settings?.hotkey} /></p>
        <button className={`agent-chip agent-${status}`} onClick={() => setPage('settings')} title="Open agent settings"><span className="status-dot" aria-hidden="true" />Agent {status}</button>
      </div>
    </aside>
    <main id="main-content" className="workspace" tabIndex={-1}>
      {(error || notice || state?.warnings.length) ? <div className="workspace-banners">
        <Feedback error={error} notice={notice} clearError={() => setError('')} clearNotice={() => setNotice('')} />
        {state?.warnings.length ? <div className="banner banner-warn" role="status"><div><strong>The agent needs attention</strong><ul>{state.warnings.map((warning, i) => <li key={i}>{warning}</li>)}</ul></div></div> : null}
      </div> : null}
      {page === 'dictations' ? <Dictations key={connectionKey} settings={settings} /> : page === 'meetings' ? <Meetings key={connectionKey} state={state} settings={settings} liveSegments={liveSegments} clearLive={clearLive} /> : page === 'vocabulary' ? <VocabularyPanel key={connectionKey} /> : <SettingsPanel />}
    </main>
  </div>;
}

createRoot(document.getElementById('root')!).render(<ErrorBoundary><Main /></ErrorBoundary>);
