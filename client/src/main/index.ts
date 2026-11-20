import { app, BrowserWindow, dialog, ipcMain, Menu, nativeImage, nativeTheme, Notification, screen, session, Tray } from 'electron'
import type { IpcMainInvokeEvent } from 'electron'
import { mkdir, rm, writeFile } from 'node:fs/promises'
import { basename, dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'
import { AgentSupervisor } from './agent-supervisor'
import { HttpServerApi } from './server-api'
import { findCorrections } from './corrections'
import { SettingsStore, toAgentConfig, validateSettings } from './settings'
import type {
  AgentEvent, AgentStatus, ClientEvent, Correction, CorrectionReply, Meeting, Page,
  RuntimeState, Settings, VocabInput, VocabSuggestion, DictationResult } from '../shared/types'

if (process.platform === 'linux' && (process.env.WAYLAND_DISPLAY || process.env.XDG_SESSION_TYPE === 'wayland')) {
  app.commandLine.appendSwitch('ozone-platform-hint', 'auto')
}
const singleInstance = app.requestSingleInstanceLock()
if (!singleInstance) app.quit()

const outputDirectory = dirname(fileURLToPath(import.meta.url))
let settingsStore: SettingsStore
let api: HttpServerApi
let agent: AgentSupervisor
let mainWindow: BrowserWindow | null = null
let overlayWindow: BrowserWindow | null = null
let vocabWindow: BrowserWindow | null = null
let tray: Tray | null = null
let quitting = false
let readyToQuit = false
let serverOnline = false
let healthTimer: ReturnType<typeof setInterval> | undefined
let overlayTimer: ReturnType<typeof setTimeout> | undefined
let overlayHeld = false
let overlayFinal = false
let dictationPending = false
let meetingStarting = false
let meetingStopping = false
let suggestionResolving = false
let activeServerRequests = 0
let suggestion: VocabSuggestion | null = null
const suggestionQueue: VocabSuggestion[] = []
let recent: Array<{ id: number | null; text: string }> = []
let state: RuntimeState = {
  agentStatus: 'offline', hello: null, warnings: [], recording: false,
  activeMeetingId: null, devices: { inputs: [], outputs: [] }, lastResult: null
}

function broadcast(event: ClientEvent): void {
  for (const win of [mainWindow, overlayWindow, vocabWindow]) {
    if (win && !win.isDestroyed()) win.webContents.send('client:event', event)
  }
}
function publishState(): void { broadcast({ type: 'state', state }); updateTray() }
function notice(error: unknown): void {
  const message = error instanceof Error ? error.message : String(error)
  console.error('[client]', message)
  broadcast({ type: 'notice', message })
}
function run(task: Promise<unknown>): void { void task.catch(notice) }
async function withServerRequest<T>(task: () => T | Promise<T>): Promise<T> {
  activeServerRequests++
  try { return await task() } finally { activeServerRequests-- }
}

function makeWindow(options: Electron.BrowserWindowConstructorOptions): BrowserWindow {
  const win = new BrowserWindow({
    show: false, backgroundColor: nativeTheme.shouldUseDarkColors ? '#101613' : '#f6f7f6', autoHideMenuBar: true,
    ...options,
    webPreferences: {
      preload: join(outputDirectory, '../preload/index.cjs'),
      contextIsolation: true, nodeIntegration: false, sandbox: true, webSecurity: true
    }
  })
  win.webContents.setWindowOpenHandler(() => ({ action: 'deny' }))
  win.webContents.on('will-navigate', event => event.preventDefault())
  win.webContents.on('will-attach-webview', event => event.preventDefault())
  return win
}
async function loadWindow(win: BrowserWindow, filename: string, query?: Record<string, string>): Promise<void> {
  if (!app.isPackaged && process.env.ELECTRON_RENDERER_URL) {
    const base = `${process.env.ELECTRON_RENDERER_URL.replace(/\/$/, '')}/`
    const url = new URL(filename, base)
    for (const [key, value] of Object.entries(query ?? {})) url.searchParams.set(key, value)
    await win.loadURL(url.href)
  } else await win.loadFile(join(outputDirectory, '../renderer', filename), { query })
}
async function openMain(page?: Page): Promise<void> {
  if (!mainWindow || mainWindow.isDestroyed()) {
    mainWindow = makeWindow({ width: 1120, height: 780, minWidth: 740, minHeight: 560, title: 'transcriptd' })
    mainWindow.on('close', event => {
      if (!quitting && tray) { event.preventDefault(); mainWindow?.hide() }
    })
    mainWindow.on('closed', () => { mainWindow = null })
    await loadWindow(mainWindow, 'index.html', { page: page ?? (settingsStore.get().token ? 'dictations' : 'settings') })
  }
  if (mainWindow.isMinimized()) mainWindow.restore()
  mainWindow.show()
  mainWindow.focus()
  if (page) mainWindow.webContents.send('client:event', { type: 'navigate', page } satisfies ClientEvent)
}
function positionOverlay(): void {
  if (!overlayWindow) return
  const { x, y, width, height } = screen.getPrimaryDisplay().workArea
  const windowWidth = Math.min(700, Math.max(340, width - 48))
  const windowHeight = Math.min(270, height - 48)
  overlayWindow.setBounds({ x: Math.round(x + (width - windowWidth) / 2), y: y + height - windowHeight - 30, width: windowWidth, height: windowHeight })
}
async function createAuxiliaryWindows(): Promise<void> {
  overlayWindow = makeWindow({
    width: 700, height: 270, frame: false, transparent: true, backgroundColor: '#00000000',
    alwaysOnTop: true, skipTaskbar: true, resizable: false, title: 'transcriptd dictation'
  })
  overlayWindow.setAlwaysOnTop(true, 'floating')
  positionOverlay()
  overlayWindow.on('closed', () => { overlayWindow = null })
  overlayWindow.on('close', event => { if (!quitting) { event.preventDefault(); overlayWindow?.hide() } })
  vocabWindow = makeWindow({ width: 460, height: 330, alwaysOnTop: true, skipTaskbar: true, resizable: false, title: 'Remember a word' })
  vocabWindow.on('close', event => {
    if (!quitting) { event.preventDefault(); if (!suggestionResolving) run(resolveSuggestion('skip')) }
  })
  vocabWindow.on('closed', () => { vocabWindow = null })
  await Promise.all([loadWindow(overlayWindow, 'overlay.html'), loadWindow(vocabWindow, 'vocab.html')])
  screen.on('display-metrics-changed', positionOverlay)
}
/** Keep the result on screen only when there is something worth a glance, unless the user asked for always/never. */
function shouldShowResult(result: DictationResult): boolean {
  const mode = settingsStore.get().resultDisplay
  if (mode === 'always') return true
  if (mode === 'never') return false
  const reverted = (result.edits ?? []).some(e => !e.accepted)
  return reverted || (result.replacements ?? []).length > 0 || (result.low_confidence ?? []).length > 0
}
function showOverlay(): void {
  clearTimeout(overlayTimer)
  if (settingsStore.get().overlayEnabled) overlayWindow?.showInactive()
}
function dismissOverlayLater(): void {
  clearTimeout(overlayTimer)
  if (!overlayHeld && overlayFinal) overlayTimer = setTimeout(() => {
    overlayWindow?.hide(); overlayFinal = false
  }, 4000)
}
function offerVocabulary(reply: CorrectionReply, correction: Correction): void {
  if (!reply.suggest) return
  const item = { term: reply.term || correction.meant, heard_as: reply.heard_as || correction.heard }
  if ([suggestion, ...suggestionQueue].some(s => s?.term === item.term && s.heard_as === item.heard_as)) return
  suggestionQueue.push(item)
  if (!suggestion) nextSuggestion()
}
function nextSuggestion(): void {
  suggestion = suggestionQueue.shift() ?? null
  broadcast({ type: 'suggestion', suggestion })
  if (suggestion) { vocabWindow?.show(); vocabWindow?.focus() }
  else vocabWindow?.hide()
}
async function resolveSuggestion(action: unknown): Promise<void> {
  if (!['add', 'replace', 'skip', 'never'].includes(String(action))) throw new Error('Unknown vocabulary action')
  if (!suggestion || suggestionResolving) return
  suggestionResolving = true
  try {
    const current = suggestion
    if (action === 'add' || action === 'replace') {
      await api.addVocab({ term: current.term, aliases: [], heard_as: action === 'replace' ? [current.heard_as] : [], added_from: 'correction_prompt' })
    } else if (action === 'never') await api.suppress(current.term)
    nextSuggestion()
  } finally { suggestionResolving = false }
}
function rememberResult(id: number | null, text: string): void {
  if (!text.trim()) return
  recent = [{ id, text }, ...recent.filter(row => id === null || row.id !== id)].slice(0, 5)
}
async function submitCorrection(input: Correction & { finalText?: string }): Promise<CorrectionReply> {
  const reply = await api.correction(input)
  if (input.finalText !== undefined && input.dictation_id != null) {
    await api.setFinal(input.dictation_id, input.finalText)
    rememberResult(input.dictation_id, input.finalText)
  }
  if (input.finalText !== undefined && state.lastResult && state.lastResult.dictation_id === input.dictation_id) {
    // Keep the ASR words and cleanup audit unchanged. Only the user's final text changes.
    state.lastResult = { ...state.lastResult, text: input.finalText }
    publishState()
  }
  offerVocabulary(reply, input)
  return reply
}
/** Corrections observed in the target app after a paste. Silent: the server learns; we only toast when a term is added. */
async function learnReadback(dictationId: number | null, corrections: Array<{heard: string; meant: string}>): Promise<void> {
  for (const c of corrections) {
    const reply = await api.correction({ heard: c.heard, meant: c.meant, dictation_id: dictationId ?? undefined, source: 'readback' })
    if (reply.learned && reply.term && Notification.isSupported()) {
      new Notification({ title: 'transcriptd', body: `Learned “${reply.term}”`, silent: true }).show()
    }
  }
}
async function learnSelection(text: string): Promise<void> {
  if (!text.trim()) return
  const changes = findCorrections(text, recent)
  for (const change of changes) {
    const reply = await api.correction({ ...change, source: 'selection' })
    offerVocabulary(reply, change)
  }
  if (!changes.length) broadcast({ type: 'notice', message: 'No matching substitution found in the last five dictations. Select the corrected sentence, not only the new word.' })
}
function waitForAgentEvent(matches: (event: AgentEvent) => boolean, send: () => void, timeoutMs: number, rejectErrors = true): Promise<void> {
  return new Promise((resolve, reject) => {
    const finish = (error?: Error): void => {
      clearTimeout(timer)
      agent.off('event', onEvent)
      agent.off('status', onStatus)
      if (error) reject(error)
      else resolve()
    }
    const onEvent = (event: AgentEvent): void => {
      if (matches(event)) finish()
      else if (rejectErrors && event.type === 'error') finish(new Error(event.message))
    }
    const onStatus = (status: AgentStatus): void => {
      if (status !== 'online') finish(new Error('The agent disconnected before confirming the command'))
    }
    const timer = setTimeout(() => finish(new Error('The agent did not confirm the command in time')), timeoutMs)
    agent.on('event', onEvent)
    agent.on('status', onStatus)
    try { send() } catch (error) { finish(error instanceof Error ? error : new Error(String(error))) }
  })
}
async function startMeeting(title = ''): Promise<Meeting> {
  if (state.activeMeetingId !== null || meetingStarting || meetingStopping) throw new Error('A meeting is already recording or stopping')
  if (state.agentStatus !== 'online') throw new Error('The agent is offline')
  if (dictationPending || state.recording) throw new Error('Finish dictation before recording a meeting')
  meetingStarting = true
  try {
    const meeting = await api.createMeeting({ title, profile: settingsStore.get().profile })
    state.activeMeetingId = meeting.id
    let started = false
    let completed = false
    try {
      // Commands run sequentially. Pong confirms that meeting.start returned;
      // error events alone can be recoverable streaming or loopback warnings.
      await waitForAgentEvent(event => {
        if (event.type === 'meeting.started' && (event.meeting_id == null || event.meeting_id === meeting.id)) started = true
        if (event.type === 'pong') { completed = true; return true }
        return false
      }, () => {
        agent.send({ type: 'meeting.start', meeting_id: meeting.id, title })
        agent.send({ type: 'ping' })
      }, 30_000, false)
      if (!started) throw new Error('The agent could not start this meeting. Check its error message and audio devices.')
    } catch (error) {
      if (completed || state.agentStatus !== 'online') state.activeMeetingId = null
      publishState()
      throw error
    }
    publishState()
    return meeting
  } finally { meetingStarting = false; updateTray() }
}
async function stopMeeting(): Promise<void> {
  if (state.activeMeetingId === null) throw new Error('No meeting is recording')
  if (meetingStarting) throw new Error('Wait for the agent to finish starting this meeting')
  if (meetingStopping) return
  meetingStopping = true
  updateTray()
  let stopped = false
  try {
    await waitForAgentEvent(event => {
      if (event.type === 'meeting.stopped') stopped = true
      return event.type === 'pong'
    }, () => {
      agent.send({ type: 'meeting.stop' })
      agent.send({ type: 'ping' })
    }, 180_000, false)
    // Even a failed stop consumes the agent's meeting handle. Pong confirms it
    // has finished before we release the server connection and recording guards.
    state.activeMeetingId = null
    if (!stopped) throw new Error('The agent stopped without confirming a saved WAV. Check its error message and local meeting files.')
  } finally {
    meetingStopping = false
    publishState()
  }
}
function onAgentEvent(event: AgentEvent): void {
  switch (event.type) {
    case 'hello': state.hello = event; break
    case 'configured': state.warnings = event.warnings; break
    case 'devices': state.devices = { inputs: event.inputs, outputs: event.outputs }; break
    case 'ptt.down':
      state.recording = true; dictationPending = true; overlayFinal = false; overlayHeld = false; showOverlay(); break
    case 'ptt.up': state.recording = false; break
    case 'final':
      state.recording = false
      dictationPending = false
      state.lastResult = event.result
      rememberResult(event.result.dictation_id ?? null, event.result.text)
      overlayFinal = Boolean(event.result.text) && shouldShowResult(event.result)
      if (overlayFinal) { showOverlay(); dismissOverlayLater() }
      else overlayWindow?.hide()
      break
    case 'selection': if (event.text) run(withServerRequest(() => learnSelection(event.text!))); break
    case 'corrections': run(withServerRequest(() => learnReadback(event.dictation_id, event.corrections))); break
    case 'meeting.started': state.activeMeetingId = event.meeting_id ?? state.activeMeetingId; break
    case 'meeting.stopped': state.activeMeetingId = null; break
    case 'error':
      notice(event.message)
      state.recording = false
      dictationPending = false
      if (!overlayFinal) overlayWindow?.hide()
      break
  }
  broadcast({ type: 'agent', event })
  if (event.type !== 'level' && event.type !== 'meeting.level' && event.type !== 'partial' && event.type !== 'meeting.segment') publishState()
}
function updateTray(): void {
  if (!tray) return
  const recording = state.recording || state.activeMeetingId !== null
  const status = recording ? 'Recording' : state.agentStatus === 'online' && serverOnline ? 'Idle' : 'Offline'
  tray.setToolTip(`transcriptd · ${status}`)
  tray.setContextMenu(Menu.buildFromTemplate([
    { label: `transcriptd · ${status}`, enabled: false },
    { type: 'separator' },
    state.activeMeetingId !== null
      ? { label: meetingStopping ? 'Stopping meeting…' : 'Stop meeting', enabled: !meetingStopping, click: () => run(stopMeeting()) }
      : { label: 'Start meeting', enabled: state.agentStatus === 'online' && !state.recording && !meetingStarting, click: () => run(startMeeting().then(() => openMain('meetings'))) },
    { label: 'Open transcriptd', click: () => run(openMain()) },
    { label: 'Settings', click: () => run(openMain('settings')) },
    { type: 'separator' },
    { label: 'Quit', click: () => app.quit() }
  ]))
}
async function checkHealth(): Promise<void> {
  const connection = settingsStore.get()
  let online = false
  try { online = (await api.health(connection)).ok } catch { /* Offline until the next check. */ }
  const current = settingsStore.get()
  if (connection.serverUrl === current.serverUrl && connection.token === current.token) {
    serverOnline = online
    updateTray()
  }
}
async function applyLoginSetting(enabled: boolean): Promise<void> {
  if (process.platform === 'win32') {
    app.setLoginItemSettings({ openAtLogin: enabled, path: process.execPath, args: app.isPackaged ? [] : [app.getAppPath()] })
    return
  }
  if (process.platform === 'linux') {
    const directory = join(process.env.XDG_CONFIG_HOME || join(app.getPath('home'), '.config'), 'autostart')
    const path = join(directory, 'transcriptd.desktop')
    if (!enabled) { await rm(path, { force: true }); return }
    const quote = (value: string): string => `"${value.replace(/[\\"`$]/g, '\\$&').replace(/%/g, '%%')}"`
    const executable = process.env.APPIMAGE || process.execPath
    const command = [executable, ...(!app.isPackaged ? [app.getAppPath()] : [])].map(quote).join(' ')
    await mkdir(directory, { recursive: true })
    await writeFile(path, `[Desktop Entry]\nType=Application\nName=transcriptd\nExec=${command}\nTerminal=false\nX-GNOME-Autostart-enabled=true\n`, { mode: 0o600 })
  }
}

// IPC accepts only our top-level windows. Renderers never receive filesystem or process APIs.
function trusted(event: IpcMainInvokeEvent): void {
  if (![mainWindow, overlayWindow, vocabWindow].some(win => win && !win.isDestroyed() && win.webContents === event.sender) || event.senderFrame !== event.sender.mainFrame) {
    throw new Error('Untrusted IPC sender')
  }
}
function handle(channel: string, handler: (...args: unknown[]) => unknown): void {
  ipcMain.handle(channel, (event, ...args: unknown[]) => {
    trusted(event)
    if (['server:call', 'correction:submit', 'suggestion:resolve'].includes(channel)) {
      return withServerRequest(() => handler(...args))
    }
    return handler(...args)
  })
}
function string(value: unknown, name: string, max = 1_000_000): string {
  if (typeof value !== 'string' || value.length > max || value.includes('\0')) throw new Error(`Invalid ${name}`)
  return value
}
function id(value: unknown): number {
  if (typeof value !== 'number' || !Number.isSafeInteger(value) || value < 1) throw new Error('Invalid ID')
  return value
}
function object(value: unknown): Record<string, unknown> {
  if (value === null || typeof value !== 'object' || Array.isArray(value)) throw new Error('Invalid request')
  return value as Record<string, unknown>
}
function vocabInput(value: unknown): VocabInput {
  const input = object(value)
  const list = (value: unknown): string[] => {
    if (!Array.isArray(value) || value.length > 1000) throw new Error('Invalid vocabulary list')
    return value.map(item => string(item, 'vocabulary entry', 1000))
  }
  const term = string(input.term, 'term', 200).trim()
  if (!term) throw new Error('A term is required')
  return { term, aliases: list(input.aliases), heard_as: list(input.heard_as), language: input.language == null ? null : string(input.language, 'language', 100), added_from: typeof input.added_from === 'string' ? string(input.added_from, 'source', 200) : 'manual' }
}
function correctionInput(value: unknown): Correction & { finalText?: string } {
  const input = object(value)
  return {
    heard: string(input.heard, 'heard text', 10000), meant: string(input.meant, 'corrected text', 10000),
    dictation_id: input.dictation_id == null ? null : id(input.dictation_id),
    source: input.source === undefined ? 'overlay_edit' : string(input.source, 'source', 200),
    ...(input.finalText === undefined ? {} : { finalText: string(input.finalText, 'final text') })
  }
}
function page(value: unknown): Page | undefined {
  if (value === undefined) return undefined
  if (!['dictations', 'meetings', 'vocabulary', 'settings'].includes(String(value))) throw new Error('Invalid page')
  return value as Page
}
function registerIpc(): void {
  handle('settings:get', () => settingsStore.get())
  handle('settings:save', async value => {
    const settings = validateSettings(value)
    const previous = settingsStore.get()
    const connectionChanged = settings.serverUrl !== previous.serverUrl || settings.token !== previous.token
    if (connectionChanged && (dictationPending || state.recording || state.activeMeetingId !== null || meetingStarting || meetingStopping || suggestionResolving || activeServerRequests > 0)) {
      throw new Error('Finish recording, transcription, and pending server requests before changing the server connection')
    }
    if (settings.startAtLogin !== previous.startAtLogin) await applyLoginSetting(settings.startAtLogin)
    settingsStore.save(settings)
    if (connectionChanged) {
      serverOnline = false
      recent = []; suggestionQueue.length = 0; suggestion = null; state.lastResult = null
      overlayWindow?.hide(); vocabWindow?.hide()
      broadcast({ type: 'suggestion', suggestion: null })
      publishState()
    }
    if (state.agentStatus === 'online') agent.send({ type: 'configure', config: toAgentConfig(settings) })
    if (!settings.overlayEnabled) overlayWindow?.hide()
    broadcast({ type: 'settings', settings })
    run(checkHealth())
    return settings
  })
  handle('state:get', () => state)
  handle('agent:restart', async () => {
    if (dictationPending || state.recording || state.activeMeetingId !== null || meetingStarting || meetingStopping) throw new Error('Finish recording and transcription before restarting the agent')
    await agent.restart()
  })
  handle('agent:devices', () => { agent.send({ type: 'devices.list' }) })
  handle('agent:inject', async value => {
    const text = string(value, 'injection text')
    if (state.agentStatus !== 'online') throw new Error('The agent is offline')
    mainWindow?.hide(); overlayWindow?.hide()
    // The user chooses the target during this delay. Never try to refocus a Wayland app.
    await new Promise(resolve => setTimeout(resolve, 1500))
    agent.send({ type: 'inject', text })
  })
  handle('meeting:start', title => startMeeting(title === undefined ? '' : string(title, 'meeting title', 1000)))
  handle('meeting:stop', stopMeeting)
  handle('correction:submit', value => submitCorrection(correctionInput(value)))
  handle('suggestion:get', () => suggestion)
  handle('suggestion:resolve', resolveSuggestion)
  handle('overlay:hold', value => {
    if (typeof value !== 'boolean') throw new Error('Invalid hold state')
    overlayHeld = value
    if (overlayHeld) clearTimeout(overlayTimer)
    else dismissOverlayLater()
  })
  handle('window:main', value => openMain(page(value)))
  handle('file:save', async (name, value) => {
    const defaultPath = basename(string(name, 'filename', 200))
    const text = string(value, 'file text', 10_000_000)
    const result = await dialog.showSaveDialog({ defaultPath, filters: [{ name: 'Text', extensions: ['md', 'txt'] }] })
    if (result.canceled || !result.filePath) return false
    await writeFile(result.filePath, text, 'utf8')
    return true
  })
  handle('server:call', (method, value) => {
    if (!Array.isArray(value) || value.length > 3) throw new Error('Invalid API arguments')
    const [a, b] = value as unknown[]
    const query = (v: unknown): string | undefined => v === undefined ? undefined : string(v, 'search', 1000)
    switch (method) {
      case 'health': {
        if (a === undefined) return api.health()
        const input = object(a)
        const settings = validateSettings({ ...settingsStore.get(), serverUrl: input.serverUrl, token: input.token })
        return api.health(settings)
      }
      case 'dictations': return api.dictations(query(a))
      case 'dictation': return api.dictation(id(a))
      case 'setFinal': return api.setFinal(id(a), string(b, 'final text'))
      case 'vocab': return api.vocab()
      case 'addVocab': return api.addVocab(vocabInput(a))
      case 'updateVocab': return api.updateVocab(id(a), vocabInput(b))
      case 'deleteVocab': return api.deleteVocab(id(a))
      case 'importVocab': return api.importVocab(string(a, 'vocabulary text', 10_000_000))
      case 'exportVocab': return api.exportVocab()
      case 'suppress': return api.suppress(string(a, 'word', 200))
      case 'correction': return api.correction(correctionInput(a))
      case 'meetings': return api.meetings(query(a))
      case 'createMeeting': {
        const input = object(a)
        return api.createMeeting({ title: string(input.title, 'title', 1000), profile: input.profile == null ? null : string(input.profile, 'profile', 200) })
      }
      case 'meeting': return api.meeting(id(a))
      case 'patchMeeting': {
        const input = object(b)
        return api.patchMeeting(id(a), {
          ...(input.title === undefined ? {} : { title: string(input.title, 'title', 1000) }),
          ...(input.notes === undefined ? {} : { notes: string(input.notes, 'notes') })
        })
      }
      case 'deleteMeeting':
        if (state.activeMeetingId === id(a)) throw new Error('Stop this meeting before deleting it')
        return api.deleteMeeting(id(a))
      case 'transcript':
        if (b !== undefined && b !== 'md' && b !== 'txt') throw new Error('Invalid transcript format')
        return api.transcript(id(a), b as 'md' | 'txt' | undefined)
      case 'summarize': return api.summarize(id(a))
      case 'processMeeting':
        if (state.activeMeetingId === id(a)) throw new Error('Stop this meeting before processing it')
        return api.processMeeting(id(a))
      default: throw new Error('Unknown API method')
    }
  })
}

app.on('second-instance', () => { if (app.isReady()) run(openMain()) })
app.on('activate', () => { if (app.isReady()) run(openMain()) })
app.on('window-all-closed', () => { /* The tray owns the application's lifetime. */ })
app.on('before-quit', event => {
  if (readyToQuit) return
  event.preventDefault()
  if (quitting) return
  quitting = true
  clearInterval(healthTimer); clearTimeout(overlayTimer)
  const shutdown = async (): Promise<void> => {
    if (agent && state.activeMeetingId !== null && state.agentStatus === 'online') {
      try {
        await waitForAgentEvent(event => event.type === 'meeting.stopped',
          () => { if (!meetingStopping) agent.send({ type: 'meeting.stop' }) }, 30_000, false)
      } catch (error) { console.error('[shutdown]', error) }
    }
    await agent?.stop()
  }
  void shutdown().catch(notice).finally(() => {
    readyToQuit = true
    tray?.destroy(); tray = null
    app.quit()
  })
})

if (singleInstance) void app.whenReady().then(async () => {
  session.defaultSession.setPermissionRequestHandler((_contents, _permission, callback) => callback(false))
  session.defaultSession.setPermissionCheckHandler(() => false)
  settingsStore = new SettingsStore(app.getPath('userData'))
  api = new HttpServerApi(() => settingsStore.get())
  agent = new AgentSupervisor({
    getConfig: () => toAgentConfig(settingsStore.get()), appRoot: app.getAppPath(),
    resourcesPath: process.resourcesPath, packaged: app.isPackaged
  })
  agent.on('event', onAgentEvent)
  agent.on('log', line => console.error('[agent]', line))
  agent.on('status', status => {
    state.agentStatus = status
    if (status !== 'online') {
      const wasRecording = state.recording || state.activeMeetingId !== null
      state.recording = false; dictationPending = false; state.activeMeetingId = null; state.hello = null
      meetingStopping = false
      overlayWindow?.hide()
      if (wasRecording) notice('The agent disconnected during recording. Check the local meeting WAV before starting again.')
    }
    publishState()
  })
  registerIpc()
  await createAuxiliaryWindows()
  try {
    const icon = nativeImage.createFromPath(join(app.getAppPath(), 'resources/tray.png'))
    tray = new Tray(icon)
    tray.on('click', () => run(openMain()))
    updateTray()
  } catch (error) { notice(error) }
  await openMain(settingsStore.get().token ? 'dictations' : 'settings')
  agent.start()
  run(checkHealth())
  healthTimer = setInterval(() => run(checkHealth()), 30_000)
  const initialConnection = settingsStore.get()
  void api.dictations().then(rows => {
    const current = settingsStore.get()
    if (!recent.length && initialConnection.serverUrl === current.serverUrl && initialConnection.token === current.token) {
      recent = rows.slice(0, 5).map(row => ({ id: row.id, text: row.text }))
    }
  }).catch(() => { /* First launch has no token; Settings explains how to connect. */ })
}).catch(error => {
  console.error(error)
  dialog.showErrorBox('transcriptd could not start', error instanceof Error ? error.message : String(error))
  app.quit()
})
