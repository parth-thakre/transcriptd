import { describe, expect, test } from 'bun:test'
import { mkdtempSync, readFileSync, rmSync, statSync, writeFileSync } from 'node:fs'
import { join } from 'node:path'
import { defaultSettings, SettingsStore, toAgentConfig, validateSettings } from '../src/main/settings'

const clientRoot = new URL('..', import.meta.url).pathname

describe('settings', () => {
  test('maps desktop settings onto Rust fields', () => {
    const mapped = toAgentConfig(defaultSettings)
    expect(mapped.server_url).toBe(defaultSettings.serverUrl)
    expect(mapped.correction_hotkey).toBe(defaultSettings.correctionHotkey)
    expect(mapped.inject.restore_delay_ms).toBe(400)
    expect(mapped.partials).toBe(false)
  })
  test('rejects non-HTTP servers and invalid settings', () => {
    expect(() => validateSettings({ ...defaultSettings, serverUrl: 'file:///etc/passwd' })).toThrow()
    expect(() => validateSettings({ ...defaultSettings, serverUrl: 'http://user:pass@localhost' })).toThrow()
    expect(() => validateSettings({ ...defaultSettings, autoInject: 'yes' })).toThrow()
    expect(() => validateSettings({ ...defaultSettings, inject: { ...defaultSettings.inject, restore_delay_ms: -1 } })).toThrow()
    expect(() => validateSettings({ ...defaultSettings, hotkey: '' })).toThrow()
  })
  test('validates hotkey grammar before it can poison agent startup', () => {
    for (const hotkey of ['ctrl+alt', 'space+v', '++']) {
      expect(() => validateSettings({ ...defaultSettings, hotkey })).toThrow()
      expect(() => validateSettings({ ...defaultSettings, correctionHotkey: hotkey })).toThrow()
    }
    expect(validateSettings({ ...defaultSettings, hotkey: 'RightAlt', correctionHotkey: '' }).hotkey).toBe('rightalt')
    expect(validateSettings({ ...defaultSettings, correctionHotkey: '' }).correctionHotkey).toBeNull()
  })
  test('persists atomically and returns copies', () => {
    const directory = mkdtempSync(join(clientRoot, '.smoke-settings-'))
    try {
      const store = new SettingsStore(directory)
      const settings = store.save({ ...defaultSettings, token: 'test-secret', showPartials: true })
      settings.token = 'changed'
      expect(store.get().token).toBe('test-secret')
      const restored = new SettingsStore(directory)
      expect(restored.get().showPartials).toBe(true)
      expect(readFileSync(join(directory, 'settings.json'), 'utf8')).toContain('test-secret')
      if (process.platform !== 'win32') expect(statSync(join(directory, 'settings.json')).mode & 0o777).toBe(0o600)
    } finally { rmSync(directory, { recursive: true, force: true }) }
  })
  test('leaves damaged settings untouched', () => {
    const directory = mkdtempSync(join(clientRoot, '.smoke-settings-'))
    try {
      writeFileSync(join(directory, 'settings.json'), 'not JSON')
      expect(new SettingsStore(directory).get().serverUrl).toBe(defaultSettings.serverUrl)
      expect(readFileSync(join(directory, 'settings.json'), 'utf8')).toBe('not JSON')
    } finally { rmSync(directory, { recursive: true, force: true }) }
  })
})
