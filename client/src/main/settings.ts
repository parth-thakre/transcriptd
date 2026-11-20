import { existsSync, mkdirSync, readFileSync, renameSync, writeFileSync } from 'node:fs'
import { dirname, join } from 'node:path'
import type { AgentConfig, Settings } from '../shared/types'

export const defaultSettings: Settings = {
  serverUrl: 'http://127.0.0.1:7411', token: '', profile: null,
  hotkey: 'ctrl+alt+space', correctionHotkey: 'ctrl+alt+shift+v', hotkeyMode: 'hold',
  autoInject: true,
  inject: { method: 'clipboard', paste_chord: null, restore_clipboard: true, restore_delay_ms: 400, trailing_space: true },
  micDevice: null, loopbackDevice: null, overlayEnabled: true, showPartials: false, startAtLogin: false,
  resultDisplay: 'never'
}

function record(value: unknown): Record<string, unknown> {
  if (typeof value !== 'object' || value === null || Array.isArray(value)) throw new Error('Expected a settings object')
  return value as Record<string, unknown>
}
function text(value: unknown, field: string, max = 2000): string {
  if (typeof value !== 'string' || value.length > max || value.includes('\0')) throw new Error(`Invalid ${field}`)
  return value
}
function optionalText(value: unknown, field: string): string | null {
  return value === null || value === '' ? null : text(value, field)
}
function bool(value: unknown, field: string): boolean {
  if (typeof value !== 'boolean') throw new Error(`Invalid ${field}`)
  return value
}
function validateChord(value: string, field: string): string {
  const modifiers = new Set(['ctrl', 'control', 'alt', 'option', 'shift', 'super', 'meta', 'win', 'cmd'])
  const parts = value.toLowerCase().split('+').map(part => part.trim()).filter(Boolean)
  if (parts.filter(part => !modifiers.has(part)).length !== 1) {
    throw new Error(`${field} must contain exactly one main key, for example ctrl+alt+space or rightalt`)
  }
  return parts.join('+')
}
export function validateSettings(value: unknown): Settings {
  const s = record(value)
  const url = new URL(text(s.serverUrl, 'server URL'))
  if (!['http:', 'https:'].includes(url.protocol) || url.username || url.password || url.search || url.hash) {
    throw new Error('Use an HTTP(S) server URL without credentials, query, or fragment')
  }
  const inject = record(s.inject)
  if (s.hotkeyMode !== 'hold' && s.hotkeyMode !== 'toggle') throw new Error('Invalid hotkey mode')
  if (inject.method !== 'clipboard' && inject.method !== 'type') throw new Error('Invalid injection method')
  if (inject.paste_chord !== null && !['shift+insert', 'ctrl+v', 'ctrl+shift+v'].includes(String(inject.paste_chord))) throw new Error('Invalid paste chord')
  if (!Number.isInteger(inject.restore_delay_ms) || Number(inject.restore_delay_ms) < 0 || Number(inject.restore_delay_ms) > 10000) throw new Error('Clipboard restore delay must be 0–10000 ms')
  const hotkey = validateChord(text(s.hotkey, 'hotkey', 200).trim(), 'Push-to-talk hotkey')
  const correctionHotkey = optionalText(s.correctionHotkey, 'correction hotkey')
  return {
    serverUrl: url.toString().replace(/\/$/, ''), token: text(s.token, 'token', 10000).trim(),
    profile: optionalText(s.profile, 'profile'), hotkey,
    correctionHotkey: correctionHotkey === null ? null : validateChord(correctionHotkey, 'Correction hotkey'), hotkeyMode: s.hotkeyMode,
    autoInject: bool(s.autoInject, 'auto-inject'),
    inject: {
      method: inject.method, paste_chord: inject.paste_chord as string | null,
      restore_clipboard: bool(inject.restore_clipboard, 'restore clipboard'),
      restore_delay_ms: Number(inject.restore_delay_ms), trailing_space: bool(inject.trailing_space, 'trailing space')
    },
    micDevice: optionalText(s.micDevice, 'microphone'), loopbackDevice: optionalText(s.loopbackDevice, 'loopback device'),
    overlayEnabled: bool(s.overlayEnabled, 'overlay'), showPartials: bool(s.showPartials, 'partials'),
    resultDisplay: s.resultDisplay === 'always' || s.resultDisplay === 'flagged' ? s.resultDisplay : 'never',
    startAtLogin: bool(s.startAtLogin, 'start at login')
  }
}
export function toAgentConfig(s: Settings): AgentConfig {
  return {
    server_url: s.serverUrl, token: s.token, profile: s.profile, language: null,
    hotkey: s.hotkey, correction_hotkey: s.correctionHotkey, hotkey_mode: s.hotkeyMode,
    auto_inject: s.autoInject, inject: s.inject, mic_device: s.micDevice,
    loopback_device: s.loopbackDevice, partials: s.showPartials
  }
}
/**
 * First-run connection seed: a `transcriptd.json` next to the executable (portable
 * builds set PORTABLE_EXECUTABLE_DIR) or TRANSCRIPTD_SERVER_URL / TRANSCRIPTD_TOKEN in
 * the environment. Only consulted when no settings file exists yet.
 */
export function seedConnection(env: NodeJS.ProcessEnv = process.env, execPath: string = process.execPath): Partial<Settings> {
  const seed: Partial<Settings> = {}
  const dirs = [env.PORTABLE_EXECUTABLE_DIR, dirname(execPath), process.cwd()].filter((d): d is string => !!d)
  for (const dir of dirs) {
    const file = join(dir, 'transcriptd.json')
    if (!existsSync(file)) continue
    try {
      const raw = record(JSON.parse(readFileSync(file, 'utf8')))
      if (typeof raw.serverUrl === 'string') seed.serverUrl = raw.serverUrl
      if (typeof raw.token === 'string') seed.token = raw.token
      if (typeof raw.profile === 'string') seed.profile = raw.profile
      console.log(`[settings] Seeded connection from ${file}`)
      break
    } catch {
      console.warn(`[settings] Ignoring unreadable ${file}`)
    }
  }
  if (env.TRANSCRIPTD_SERVER_URL) seed.serverUrl = env.TRANSCRIPTD_SERVER_URL
  if (env.TRANSCRIPTD_TOKEN) seed.token = env.TRANSCRIPTD_TOKEN
  return seed
}

export class SettingsStore {
  private settings: Settings
  private readonly path: string
  constructor(private readonly directory: string) {
    this.path = join(directory, 'settings.json')
    this.settings = structuredClone(defaultSettings)
    try {
      const saved = JSON.parse(readFileSync(this.path, 'utf8')) as Partial<Settings>
      this.settings = validateSettings({ ...defaultSettings, ...saved, inject: { ...defaultSettings.inject, ...saved.inject } })
    } catch (error) {
      if ((error as NodeJS.ErrnoException).code !== 'ENOENT') console.warn('[settings] Cannot read settings; using defaults. The original file was left untouched.')
      else this.settings = validateSettings({ ...this.settings, ...seedConnection() })
    }
  }
  get(): Settings { return structuredClone(this.settings) }
  save(value: unknown): Settings {
    const settings = validateSettings(value)
    mkdirSync(this.directory, { recursive: true, mode: 0o700 })
    writeFileSync(`${this.path}.tmp`, JSON.stringify(settings, null, 2), { mode: 0o600 })
    renameSync(`${this.path}.tmp`, this.path)
    this.settings = settings
    return this.get()
  }
}
