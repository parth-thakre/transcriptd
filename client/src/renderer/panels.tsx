import { Fragment, useCallback, useEffect, useRef, useState } from 'react';
import { Feedback, Keys } from './components';
import type { FormEvent } from 'react';
import type { Devices, Health, InjectOptions, RuntimeState, Settings, VocabInput, VocabTerm } from '../shared/types';

function errorMessage(error: unknown): string {
  return error instanceof Error ? error.message : String(error);
}

function optional(value: string): string | null {
  return value.trim() || null;
}

function lines(value: string): string[] {
  return [...new Set(value.split(/\r?\n/).map((line) => line.trim()).filter(Boolean))];
}

const emptyDevices: Devices = { inputs: [], outputs: [] };

export function SettingsPanel() {
  const [settings, setSettings] = useState<Settings | null>(null);
  const [saved, setSaved] = useState<Settings | null>(null);
  const [runtime, setRuntime] = useState<RuntimeState | null>(null);
  const [devices, setDevices] = useState<Devices>(emptyDevices);
  const [health, setHealth] = useState<{ value: Health; serverUrl: string; token: string } | null>(null);
  const [pending, setPending] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [loadKey, setLoadKey] = useState(0);
  const dirty = useRef(false);

  useEffect(() => {
    let active = true;
    let receivedSettings = false;
    let receivedState = false;
    let receivedDevices = false;
    setError(null);
    if (!window.transcriptd) {
      setError('The desktop bridge is unavailable. Open this page through transcriptd.');
      return;
    }
    const unsubscribe = window.transcriptd.onEvent((event) => {
      if (!active) return;
      if (event.type === 'settings') {
        receivedSettings = true;
        setSaved(event.settings);
        if (!dirty.current) setSettings(event.settings);
      } else if (event.type === 'state') {
        receivedState = true;
        receivedDevices = true;
        setRuntime(event.state);
        setDevices(event.state.devices);
      } else if (event.type === 'agent') {
        if (event.event.type === 'devices') {
          receivedDevices = true;
          setDevices(event.event);
        } else if (event.event.type === 'error') {
          setError(event.event.message);
        } else if (event.event.type === 'hello') {
          const hello = event.event;
          setRuntime((current) => current ? { ...current, hello } : current);
        } else if (event.event.type === 'configured') {
          const warnings = event.event.warnings;
          setRuntime((current) => current ? { ...current, warnings } : current);
        }
      }
    });
    void window.transcriptd.getSettings().then((value) => {
      if (!active || receivedSettings) return;
      setSaved(value);
      if (!dirty.current) setSettings(value);
    }).catch((reason: unknown) => { if (active) setError(`Could not load settings. ${errorMessage(reason)}`); });
    void window.transcriptd.getState().then((value) => {
      if (!active) return;
      if (!receivedState) setRuntime(value);
      if (!receivedDevices) setDevices(value.devices);
    }).catch((reason: unknown) => { if (active) setError(`Could not load agent status. ${errorMessage(reason)}`); });
    return () => { active = false; unsubscribe(); };
  }, [loadKey]);

  function change<K extends keyof Settings>(key: K, value: Settings[K]) {
    dirty.current = true;
    setNotice(null);
    setSettings((current) => current ? { ...current, [key]: value } : current);
  }

  function changeInject<K extends keyof InjectOptions>(key: K, value: InjectOptions[K]) {
    dirty.current = true;
    setNotice(null);
    setSettings((current) => current ? { ...current, inject: { ...current.inject, [key]: value } } : current);
  }

  async function run(action: string, work: () => Promise<void>) {
    if (pending) return;
    setPending(action);
    setError(null);
    setNotice(null);
    try { await work(); }
    catch (reason) { setError(errorMessage(reason)); }
    finally { setPending(null); }
  }

  function save(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!settings) return;
    const value = settings;
    void run('save', async () => {
      let url: URL;
      try { url = new URL(value.serverUrl.trim()); }
      catch { throw new Error('Enter a valid server URL, such as http://127.0.0.1:7411.'); }
      if (!['http:', 'https:'].includes(url.protocol)) throw new Error('The server URL must use http or https.');
      if (!value.hotkey.trim()) throw new Error('Enter a dictation hotkey.');
      if (!Number.isSafeInteger(value.inject.restore_delay_ms) || value.inject.restore_delay_ms < 0 || value.inject.restore_delay_ms > 10000) {
        throw new Error('Clipboard restore delay must be a whole number from 0 to 10000 milliseconds.');
      }
      const next = await window.transcriptd.saveSettings({
        ...value,
        serverUrl: value.serverUrl.trim(),
        hotkey: value.hotkey.trim(),
        correctionHotkey: optional(value.correctionHotkey ?? ''),
        profile: optional(value.profile ?? ''),
        micDevice: optional(value.micDevice ?? ''),
        loopbackDevice: optional(value.loopbackDevice ?? ''),
        inject: { ...value.inject, paste_chord: optional(value.inject.paste_chord ?? '') },
      });
      dirty.current = false;
      setSettings(next);
      setSaved(next);
      setNotice('Settings saved. Restart the agent if you changed a hotkey.');
    });
  }

  const connectionHealth = settings && health?.serverUrl === settings.serverUrl.trim() && health.token === settings.token ? health.value : null;
  const busy = pending !== null;
  const profiles = Object.keys(connectionHealth?.profiles ?? {});
  const models = connectionHealth ? describeLoaded(connectionHealth.loaded) : '';

  return (
    <section className="page page-scroll" aria-labelledby="settings-title">
      <header className="page-head"><h1 id="settings-title">Settings</h1></header>
      <Feedback error={error} notice={notice} clearError={() => setError(null)} clearNotice={() => setNotice(null)} />
      {!settings ? (
        <div className="settings-loading">
          <p className="muted">{error ? 'Settings are unavailable.' : 'Loading settings…'}</p>
          {error && <button type="button" onClick={() => setLoadKey((key) => key + 1)}>Retry</button>}
        </div>
      ) : (
        <form onSubmit={save} aria-label="Settings" className="settings">
          <fieldset className="setting-group" disabled={busy}>
            <legend className="sr-only">Server</legend>
            <div className="group-intro"><h2 aria-hidden="true">Server</h2><p>Where your audio is transcribed. Nothing leaves this machine except to this address.</p></div>
            <div className="group-body">
              <div className="fields two">
                <label className="field">Server URL
                  <input type="url" required value={settings.serverUrl} placeholder="http://127.0.0.1:7411" onChange={(event) => change('serverUrl', event.target.value)} autoComplete="off" spellCheck={false} className="mono" />
                </label>
                <label className="field">Access token
                  <input type="password" value={settings.token} onChange={(event) => change('token', event.target.value)} autoComplete="off" spellCheck={false} className="mono" aria-describedby="token-help" />
                  <span className="help" id="token-help">Run <code>transcriptd token</code> on the server to print it.</span>
                </label>
              </div>
              <div className="actions">
                <button type="button" disabled={busy || !settings.serverUrl.trim()} onClick={() => {
                  const connection = { serverUrl: settings.serverUrl.trim(), token: settings.token };
                  void run('health', async () => {
                    setHealth(null);
                    const value = await window.transcriptd.server.health(connection);
                    setHealth({ value, ...connection });
                    if (!value.ok) throw new Error('The server responded, but its health check did not pass.');
                  });
                }}>{pending === 'health' ? 'Testing…' : 'Test connection'}</button>
                <span className="help">Uses the values above without saving them.</span>
              </div>
              {connectionHealth && <dl className={`health ${connectionHealth.ok ? 'is-ok' : 'is-bad'}`} role="status" aria-label="Connection test result">
                <div className="health-head"><span className="status-dot" aria-hidden="true" />{connectionHealth.ok ? 'Connected' : 'Health check failed'}</div>
                <div><dt>Server version</dt><dd>{connectionHealth.version}</dd></div>
                <div><dt>Default profile</dt><dd>{connectionHealth.default_profile}</dd></div>
                <div><dt>Loaded models</dt><dd>{models || 'None reported'}</dd></div>
                <div><dt>Vocabulary</dt><dd>{connectionHealth.vocab_terms} terms</dd></div>
                <div><dt>Summaries</dt><dd>{connectionHealth.llm_configured ? 'Language model configured' : 'No language model configured'}</dd></div>
              </dl>}
              <label className="field narrow">Profile
                {profiles.length ? <select value={settings.profile ?? ''} onChange={(event) => change('profile', event.target.value || null)} aria-describedby="profile-help">
                  <option value="">Server default ({connectionHealth?.default_profile})</option>
                  {settings.profile && !profiles.includes(settings.profile) && <option value={settings.profile}>{settings.profile} (not on this server)</option>}
                  {profiles.map((profile) => <option key={profile} value={profile}>{profile}</option>)}
                </select> : <input value={settings.profile ?? ''} placeholder="Server default" onChange={(event) => change('profile', event.target.value || null)} aria-describedby="profile-help" />}
                <span className="help" id="profile-help">{profiles.length ? 'Profiles choose the speech model and languages.' : 'Test the connection to choose from the server’s profiles.'}</span>
              </label>
            </div>
          </fieldset>

          <fieldset className="setting-group" disabled={busy}>
            <legend className="sr-only">Hotkeys</legend>
            <div className="group-intro"><h2 aria-hidden="true">Hotkeys</h2><p>Changes apply after you save and restart the agent.</p></div>
            <div className="group-body">
              <div className="fields two">
                <label className="field">Dictation
                  <input required value={settings.hotkey} placeholder="ctrl+alt+space" onChange={(event) => change('hotkey', event.target.value)} spellCheck={false} className="mono" aria-describedby="hotkey-preview" />
                  <span className="help" id="hotkey-preview"><Keys combo={settings.hotkey} /></span>
                </label>
                <label className="field">Correct a selection
                  <input value={settings.correctionHotkey ?? ''} placeholder="Disabled" onChange={(event) => change('correctionHotkey', event.target.value || null)} spellCheck={false} className="mono" aria-describedby="correction-help" />
                  <span className="help" id="correction-help">Select corrected text in your editor, then press this. Leave blank to turn it off.</span>
                </label>
              </div>
              <div className="field" role="radiogroup" aria-label="Hotkey mode">
                <span>Mode</span>
                <div className="segmented">
                  <label><input type="radio" name="hotkey-mode" value="hold" checked={settings.hotkeyMode === 'hold'} onChange={() => change('hotkeyMode', 'hold')} />Hold to record</label>
                  <label><input type="radio" name="hotkey-mode" value="toggle" checked={settings.hotkeyMode === 'toggle'} onChange={() => change('hotkeyMode', 'toggle')} />Press to start and stop</label>
                </div>
              </div>
            </div>
          </fieldset>

          <fieldset className="setting-group" disabled={busy}>
            <legend className="sr-only">Text insertion</legend>
            <div className="group-intro"><h2 aria-hidden="true">Text insertion</h2><p>How finished dictation reaches the app you are typing in.</p></div>
            <div className="group-body">
              <label className="check"><input type="checkbox" checked={settings.autoInject} onChange={(event) => change('autoInject', event.target.checked)} />Insert finished dictation automatically</label>
              <div className="fields three">
                <label className="field">Method
                  <select value={settings.inject.method} onChange={(event) => changeInject('method', event.target.value as InjectOptions['method'])} aria-describedby="method-help">
                    <option value="clipboard">Paste from clipboard</option><option value="type">Type text</option>
                  </select>
                  <span className="help" id="method-help">Typing uses the clipboard on Linux.</span>
                </label>
                <label className="field">Paste shortcut
                  <select value={settings.inject.paste_chord ?? ''} onChange={(event) => changeInject('paste_chord', event.target.value || null)}>
                    <option value="">Platform default</option><option value="shift+insert">Shift+Insert</option><option value="ctrl+v">Ctrl+V</option><option value="ctrl+shift+v">Ctrl+Shift+V</option>
                  </select>
                </label>
                <label className="field">Clipboard restore delay (ms)
                  <input type="number" min="0" max="10000" step="1" required className="num" value={Number.isNaN(settings.inject.restore_delay_ms) ? '' : settings.inject.restore_delay_ms} onChange={(event) => changeInject('restore_delay_ms', event.target.valueAsNumber)} />
                </label>
              </div>
              <label className="check"><input type="checkbox" checked={settings.inject.restore_clipboard} onChange={(event) => changeInject('restore_clipboard', event.target.checked)} />Restore the previous clipboard after pasting</label>
              <label className="check"><input type="checkbox" checked={settings.inject.trailing_space} onChange={(event) => changeInject('trailing_space', event.target.checked)} />Add a space after inserted text</label>
            </div>
          </fieldset>

          <fieldset className="setting-group" disabled={busy}>
            <legend className="sr-only">Audio devices</legend>
            <div className="group-intro"><h2 aria-hidden="true">Audio devices</h2><p>{devices.inputs.length} {devices.inputs.length === 1 ? 'input' : 'inputs'} and {devices.outputs.length} {devices.outputs.length === 1 ? 'output' : 'outputs'} reported by the agent.</p></div>
            <div className="group-body">
              <div className="fields two">
                <label className="field">Microphone
                  <input list="settings-inputs" value={settings.micDevice ?? ''} placeholder="System default" onChange={(event) => change('micDevice', event.target.value || null)} />
                  <datalist id="settings-inputs">{devices.inputs.map((device, index) => <option key={`${device.name}-${index}`} value={device.name}>{device.default ? 'System default' : device.name}</option>)}</datalist>
                </label>
                <label className="field">Meeting system audio
                  <input list="settings-outputs" value={settings.loopbackDevice ?? ''} placeholder="System default" onChange={(event) => change('loopbackDevice', event.target.value || null)} aria-describedby="loopback-help" />
                  <datalist id="settings-outputs">{devices.outputs.map((device, index) => <option key={`${device.name}-${index}`} value={device.name}>{device.default ? 'System default' : device.name}</option>)}</datalist>
                  <span className="help" id="loopback-help">An output device on Windows, or a monitor source name on Linux.</span>
                </label>
              </div>
              <div className="actions">
                <button type="button" disabled={busy} onClick={() => void run('devices', async () => {
                  await window.transcriptd.listDevices();
                  setNotice('Device scan requested. The lists update when the agent responds.');
                })}>{pending === 'devices' ? 'Requesting…' : 'Scan for devices'}</button>
              </div>
            </div>
          </fieldset>

          <fieldset className="setting-group" disabled={busy}>
            <legend className="sr-only">Desktop</legend>
            <div className="group-intro"><h2 aria-hidden="true">Desktop</h2><p>The overlay appears near the bottom of the screen and never takes focus.</p></div>
            <div className="group-body">
              <label className="check"><input type="checkbox" checked={settings.overlayEnabled} onChange={(event) => change('overlayEnabled', event.target.checked)} />Show the dictation overlay</label>
              <label className="check"><input type="checkbox" checked={settings.showPartials} onChange={(event) => change('showPartials', event.target.checked)} />Show words live while recording</label>
              <label className="field"><span>Keep the result on screen after dictating</span>
                <select value={settings.resultDisplay} onChange={(event) => change('resultDisplay', event.target.value as Settings['resultDisplay'])}>
                  <option value="flagged">Only when something was changed, replaced or uncertain</option>
                  <option value="always">Always</option>
                  <option value="never">Never</option>
                </select></label>
              <label className="check"><input type="checkbox" checked={settings.startAtLogin} onChange={(event) => change('startAtLogin', event.target.checked)} />Start transcriptd when I sign in</label>
            </div>
          </fieldset>
          <div className={`savebar ${dirty.current ? 'is-dirty' : ''}`}>
            <span className="muted">{dirty.current ? 'Unsaved changes' : 'All changes saved'}</span>
            <button type="button" className="ghost" disabled={busy || !dirty.current || !saved} onClick={() => { dirty.current = false; setSettings(saved); setNotice(null); }}>Discard</button>
            <button type="submit" className="primary" disabled={busy || !dirty.current}>{pending === 'save' ? 'Saving…' : 'Save settings'}</button>
          </div>
        </form>
      )}
      <section className="setting-group agent-group" aria-labelledby="agent-heading">
        <div className="group-intro"><h2 id="agent-heading">Local agent</h2><p>Captures audio, listens for hotkeys, and inserts text.</p></div>
        <div className="group-body">
          <p role="status" className={`agent-line agent-${runtime?.agentStatus ?? 'starting'}`}><span className="status-dot" aria-hidden="true" />{runtime ? `Agent ${runtime.agentStatus}` : 'Waiting for status'}</p>
          {runtime?.hello ? <dl className="facts">
            <div><dt>Version</dt><dd>{runtime.hello.version}</dd></div>
            <div><dt>Platform</dt><dd>{runtime.hello.platform}</dd></div>
            <div><dt>Hotkeys via</dt><dd>{runtime.hello.hotkey_backend}</dd></div>
            <div><dt>Text insertion via</dt><dd>{runtime.hello.inject_backend}</dd></div>
          </dl> : <p className="help">Details appear once the agent connects.</p>}
          {!!runtime?.warnings.length && <div className="banner banner-warn" role="status"><div><strong>Warnings</strong><ul>{runtime.warnings.map((warning, index) => <li key={index}>{warning}</li>)}</ul></div></div>}
          <div className="actions">
            <button type="button" disabled={busy || runtime?.recording || runtime?.activeMeetingId != null || runtime?.agentStatus === 'restarting'} onClick={() => void run('restart', async () => {
              await window.transcriptd.restartAgent();
              setNotice('Agent restart requested. It will use your saved settings.');
            })}>{pending === 'restart' || runtime?.agentStatus === 'restarting' ? 'Restarting…' : 'Restart agent'}</button>
            <span className="help">{runtime?.recording || runtime?.activeMeetingId != null ? 'Stop recording before restarting the agent.' : 'Restarting uses saved settings only.'}</span>
          </div>
        </div>
      </section>
    </section>
  );
}

function describeLoaded(loaded: unknown): string {
  if (loaded == null) return '';
  if (typeof loaded === 'string') return loaded;
  if (Array.isArray(loaded)) return loaded.map(item => typeof item === 'string' ? item : JSON.stringify(item)).join(', ');
  if (typeof loaded === 'object') return Object.entries(loaded as Record<string, unknown>).map(([k, v]) => typeof v === 'string' || typeof v === 'number' ? `${k}: ${v}` : k).join(', ');
  return String(loaded);
}

interface VocabDraft {
  term: string;
  aliases: string;
  heardAs: string;
  language: string;
}

const emptyDraft: VocabDraft = { term: '', aliases: '', heardAs: '', language: '' };

export function VocabularyPanel() {
  const [terms, setTerms] = useState<VocabTerm[]>([]);
  const [draft, setDraft] = useState<VocabDraft>(emptyDraft);
  const [editing, setEditing] = useState<VocabTerm | null>(null);
  const [deleting, setDeleting] = useState<number | null>(null);
  const [query, setQuery] = useState('');
  const [adding, setAdding] = useState(false);
  const [importText, setImportText] = useState('');
  const [loading, setLoading] = useState(true);
  const [pending, setPending] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const request = useRef(0);
  const termInput = useRef<HTMLInputElement>(null);

  const load = useCallback(async () => {
    const id = ++request.current;
    setLoading(true);
    try {
      const result = await window.transcriptd.server.vocab();
      if (id === request.current) setTerms(result);
    } catch (reason) {
      if (id === request.current) throw reason;
    } finally {
      if (id === request.current) setLoading(false);
    }
  }, []);

  useEffect(() => {
    void load().catch((reason: unknown) => setError(`Could not load vocabulary. ${errorMessage(reason)}`));
    return () => { request.current += 1; };
  }, [load]);

  async function run(action: string, work: () => Promise<void>) {
    if (pending) return;
    setPending(action);
    setError(null);
    setNotice(null);
    try { await work(); }
    catch (reason) { setError(errorMessage(reason)); }
    finally { setPending(null); }
  }

  function resetDraft() {
    setEditing(null);
    setAdding(false);
    setDraft(emptyDraft);
  }

  function save(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!draft.term.trim()) { setError('Enter a term before saving.'); termInput.current?.focus(); return; }
    const input: VocabInput = {
      term: draft.term.trim(), aliases: lines(draft.aliases), heard_as: lines(draft.heardAs),
      language: optional(draft.language), added_from: editing?.added_from ?? 'manual',
    };
    void run('save', async () => {
      const result = editing
        ? await window.transcriptd.server.updateVocab(editing.id, input)
        : await window.transcriptd.server.addVocab(input);
      setTerms((current) => [...current.filter((term) => term.id !== result.id), result]);
      setNotice(editing ? `Saved changes to ${result.term}.` : `Added ${result.term}.`);
      if (editing) resetDraft();
      else { setDraft(emptyDraft); termInput.current?.focus(); }
    });
  }

  const busy = pending !== null;
  const search = query.trim().toLocaleLowerCase();
  const visibleTerms = terms.filter((term) => [term.term, ...term.aliases, ...term.heard_as, term.language ?? ''].some((text) => text.toLocaleLowerCase().includes(search)))
    .sort((a, b) => a.term.localeCompare(b.term));
  const set = (key: keyof VocabDraft) => (event: { target: { value: string } }) => setDraft((current) => ({ ...current, [key]: event.target.value }));

  const editor = (label: string) => <tr className="editor-row"><td colSpan={6}>
    <form onSubmit={save} aria-label={label} className="term-editor" onKeyDown={(event) => { if (event.key === 'Escape' && !busy) { event.preventDefault(); resetDraft(); } }}>
      <label className="field">Term
        <input ref={termInput} required disabled={busy || loading} value={draft.term} onChange={set('term')} placeholder="Spelling you want" autoComplete="off" autoFocus />
      </label>
      <label className="field">Heard as
        <textarea rows={2} disabled={busy || loading} value={draft.heardAs} onChange={set('heardAs')} className="mono" placeholder="cooper netties" aria-describedby="vocab-heard-help" />
        <span className="help" id="vocab-heard-help">What the model writes by mistake, one per line.</span>
      </label>
      <label className="field">Also written as
        <textarea rows={2} disabled={busy || loading} value={draft.aliases} onChange={set('aliases')} aria-describedby="vocab-alias-help" />
        <span className="help" id="vocab-alias-help">Other accepted spellings, one per line.</span>
      </label>
      <label className="field">Language
        <input disabled={busy || loading} value={draft.language} onChange={set('language')} placeholder="Any" aria-describedby="vocab-language-help" className="mono" />
        <span className="help" id="vocab-language-help">Optional code, such as en, hi, or mr.</span>
      </label>
      <div className="actions">
        <button type="submit" className="primary small" disabled={busy || loading || !draft.term.trim()}>{pending === 'save' ? 'Saving…' : editing ? 'Save changes' : 'Add term'}</button>
        <button type="button" className="ghost small" disabled={busy} onClick={resetDraft}>{editing ? 'Cancel' : 'Close'}</button>
      </div>
    </form>
  </td></tr>;

  return (
    <section className="page page-scroll" aria-labelledby="vocabulary-title">
      <header className="page-head">
        <h1 id="vocabulary-title">Vocabulary</h1>
        <div className="page-tools">
          <label className="search inline"><span className="sr-only">Search vocabulary</span><input type="search" value={query} onChange={(event) => setQuery(event.target.value)} placeholder="Search terms and misheard phrases" /></label>
          <button type="button" className="primary" disabled={busy || loading || adding} onClick={() => { setEditing(null); setDraft(emptyDraft); setDeleting(null); setNotice(null); setAdding(true); }}>Add term</button>
        </div>
      </header>
      <Feedback error={error} notice={notice} clearError={() => setError(null)} clearNotice={() => setNotice(null)} />
      <p className="page-lede">Names and jargon the model should spell your way. When it hears a listed phrase, the word is replaced and marked in green so you can check it.</p>
      <div className="table-wrap">
        <table className="vocab-table" aria-label="Vocabulary terms" aria-busy={loading}>
          <thead><tr><th scope="col">Term</th><th scope="col">Heard as</th><th scope="col">Also written as</th><th scope="col">Language</th><th scope="col">Added from</th><th scope="col"><span className="sr-only">Actions</span></th></tr></thead>
          <tbody>
            {adding && !editing && editor('Add vocabulary term')}
            {visibleTerms.map((term) => editing?.id === term.id ? <Fragment key={term.id}>{editor(`Edit ${term.term}`)}</Fragment> : <tr key={term.id} className={deleting === term.id ? 'is-deleting' : undefined}>
              <th scope="row" className="term">{term.term}</th>
              <td>{term.heard_as.length ? <span className="chips">{term.heard_as.map((h) => <span className="chip evidence" key={h}>{h}</span>)}</span> : <span className="muted">None</span>}</td>
              <td>{term.aliases.join(', ') || <span className="muted">None</span>}</td>
              <td>{term.language ? <span className="lang">{term.language}</span> : <span className="muted">Any</span>}</td>
              <td className="muted">{term.added_from ?? 'manual'}</td>
              <td className="row-actions">
                {deleting === term.id ? <>
                  <span id={`delete-term-${term.id}`} className="sr-only">Delete this term from the server? This cannot be undone.</span>
                  <button type="button" className="danger-solid small" disabled={busy || loading} aria-describedby={`delete-term-${term.id}`} onClick={() => void run(`delete-${term.id}`, async () => {
                    await window.transcriptd.server.deleteVocab(term.id);
                    setTerms((current) => current.filter((item) => item.id !== term.id));
                    if (editing?.id === term.id) resetDraft();
                    setDeleting(null);
                    setNotice(`Deleted ${term.term}.`);
                  })}>{pending === `delete-${term.id}` ? 'Deleting…' : 'Delete'}</button>
                  <button type="button" className="ghost small" disabled={busy} onClick={() => setDeleting(null)}>Keep</button>
                </> : <>
                  <button type="button" className="ghost small" disabled={busy || loading} aria-label={`Edit ${term.term}`} onClick={() => {
                    setAdding(false);
                    setEditing(term);
                    setDraft({ term: term.term, aliases: term.aliases.join('\n'), heardAs: term.heard_as.join('\n'), language: term.language ?? '' });
                    setDeleting(null);
                    setNotice(null);
                  }}>Edit</button>
                  <button type="button" className="ghost small danger" disabled={busy || loading} aria-label={`Delete ${term.term}`} onClick={() => setDeleting(term.id)}>Delete</button>
                </>}
              </td>
            </tr>)}
          </tbody>
        </table>
        {!loading && visibleTerms.length === 0 && !adding && <div className="table-empty">
          <p>{search ? 'No term or misheard phrase matches this search.' : 'No saved terms yet. Add the names and jargon you use, or import a list.'}</p>
          {search ? <button type="button" onClick={() => setQuery('')}>Clear search</button> : <button type="button" className="primary" onClick={() => setAdding(true)}>Add term</button>}
        </div>}
      </div>
      <p className="table-foot muted" role="status">{loading ? 'Loading vocabulary…' : search ? `${visibleTerms.length} of ${terms.length} terms` : `${terms.length} ${terms.length === 1 ? 'term' : 'terms'}`}<button type="button" className="ghost small" disabled={busy || loading} onClick={() => void run('refresh', load)}>{loading ? 'Loading…' : 'Refresh'}</button></p>

      <section className="setting-group import-group" aria-labelledby="vocabulary-import-heading">
        <div className="group-intro"><h2 id="vocabulary-import-heading">Import and export</h2><p>Plain text, one term per line. Language codes are not included.</p></div>
        <form className="group-body" onSubmit={(event) => {
          event.preventDefault();
          if (!importText.trim()) return;
          void run('import', async () => {
            const result = await window.transcriptd.server.importVocab(importText);
            setImportText('');
            setNotice(`Imported ${result.added} ${result.added === 1 ? 'term' : 'terms'}.`);
            await load();
          });
        }} aria-label="Import vocabulary">
          <label className="field">Terms to import
            <textarea rows={4} className="mono" disabled={busy || loading} value={importText} onChange={(event) => setImportText(event.target.value)} placeholder={'Hyprland | | hyper land, hip land\nParth | parth thakre'} aria-describedby="vocab-import-help" />
            <span className="help" id="vocab-import-help">Format: <code>term | alias, alias | heard phrase, heard phrase</code>. Blank lines and lines starting with # are skipped.</span>
          </label>
          <div className="actions">
            <button type="submit" className="primary" disabled={busy || loading || !importText.trim()}>{pending === 'import' ? 'Importing…' : 'Import terms'}</button>
            <button type="button" disabled={busy || loading} onClick={() => void run('export', async () => {
              const text = await window.transcriptd.server.exportVocab();
              const wasSaved = await window.transcriptd.saveText('transcriptd-vocabulary.txt', text);
              setNotice(wasSaved ? 'Vocabulary exported.' : 'Export canceled.');
            })}>{pending === 'export' ? 'Exporting…' : 'Export to a file'}</button>
          </div>
        </form>
      </section>
    </section>
  );
}
