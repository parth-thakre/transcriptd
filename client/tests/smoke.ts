/** Headless smoke test. Node 22.18+ or Bun; no Electron or GUI.
 * node tests/smoke.ts --server http://127.0.0.1:7411 --token-file /path/to/token
 * Optional: --skip-api, --skip-agent, --agent /absolute/path/to/transcriptd-agent.
 * API calls are GET-only. Agent settings, data, and socket are isolated under client.
 */
import assert from 'node:assert/strict';
import { mkdir, mkdtemp, readFile, rm, writeFile } from 'node:fs/promises';
import { join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import { AgentSupervisor } from '../src/main/agent-supervisor.ts';
import { HttpServerApi } from '../src/main/server-api.ts';
import type { AgentConfig, AgentEvent } from '../src/shared/types.ts';

const appRoot = fileURLToPath(new URL('..', import.meta.url));
const args = process.argv.slice(2);
function option(name: string): string | undefined {
  const index = args.indexOf(name);
  if (index < 0) return undefined;
  const value = args[index + 1];
  if (!value || value.startsWith('--')) throw new Error(`${name} needs a value`);
  return value;
}
function event(agent: AgentSupervisor, type: AgentEvent['type']): Promise<AgentEvent> {
  return new Promise((resolveEvent, reject) => {
    const timeout = setTimeout(() => { agent.off('event', listener); reject(new Error(`Agent did not emit ${type} within 15 seconds`)); }, 15_000);
    const listener = (value: AgentEvent) => {
      if (value.type !== type) return;
      clearTimeout(timeout); agent.off('event', listener); resolveEvent(value);
    };
    agent.on('event', listener);
  });
}

async function main(): Promise<void> {
  const serverUrl = option('--server') ?? process.env.TRANSCRIPTD_SERVER ?? 'http://127.0.0.1:7411';
  const tokenFile = option('--token-file');
  const token = tokenFile ? (await readFile(resolve(tokenFile), 'utf8')).trim() : process.env.TRANSCRIPTD_TOKEN ?? '';
  if (!args.includes('--skip-api')) {
    if (!token) throw new Error('Set TRANSCRIPTD_TOKEN or pass --token-file to run the read-only API smoke test.');
    const api = new HttpServerApi(() => ({ serverUrl, token }));
    const health = await api.health();
    assert.equal(health.ok, true);
    const [dictations, meetings, vocab, exported] = await Promise.all([
      api.dictations(), api.meetings(), api.vocab(), api.exportVocab(),
    ]);
    assert(Array.isArray(dictations) && Array.isArray(meetings) && Array.isArray(vocab));
    assert.equal(typeof exported, 'string');
    if (dictations[0]) assert.equal((await api.dictation(dictations[0].id)).id, dictations[0].id);
    if (meetings[0]) {
      assert.equal((await api.meeting(meetings[0].id)).id, meetings[0].id);
      assert.equal(typeof await api.transcript(meetings[0].id, 'md'), 'string');
    }
    console.log(`API passed: health, ${dictations.length} dictations, ${meetings.length} meetings, ${vocab.length} vocabulary terms, text exports.`);
  }
  if (args.includes('--skip-agent')) return;
  if (process.platform === 'win32') {
    throw new Error('Use --skip-agent on Windows: the current agent uses a global named pipe that cannot be isolated.');
  }
  const directory = await mkdtemp(join(appRoot, '.smoke-'));
  const config: AgentConfig = {
    server_url: serverUrl, token, profile: null, language: null,
    hotkey: 'ctrl+alt+f24', correction_hotkey: null, hotkey_mode: 'hold', auto_inject: false,
    inject: { method: 'clipboard', paste_chord: null, restore_clipboard: true, restore_delay_ms: 400, trailing_space: false },
    mic_device: null, loopback_device: null, partials: false,
  };
  const env: Record<string, string> = {
    HOME: join(directory, 'home'), XDG_CONFIG_HOME: join(directory, 'config'),
    XDG_DATA_HOME: join(directory, 'data'), XDG_CACHE_HOME: join(directory, 'cache'),
    XDG_RUNTIME_DIR: join(directory, 'run'),
  };
  const binary = option('--agent');
  if (binary) env.TRANSCRIPTD_AGENT = resolve(binary);
  const prior = new Map(Object.keys(env).map(key => [key, process.env[key]]));
  const agent = new AgentSupervisor({ getConfig: () => config, appRoot, resourcesPath: appRoot, packaged: false });
  try {
    for (const path of Object.values(env).filter(value => value !== env.TRANSCRIPTD_AGENT)) await mkdir(path, { recursive: true, mode: 0o700 });
    // Rust loads config before hello; seed safe hotkeys before launching it.
    const configDirs = [join(env.XDG_CONFIG_HOME, 'transcriptd')];
    if (process.platform === 'darwin') configDirs.push(join(env.HOME, 'Library', 'Application Support', 'transcriptd'));
    for (const path of configDirs) {
      await mkdir(path, { recursive: true, mode: 0o700 });
      await writeFile(join(path, 'agent.json'), JSON.stringify(config), { mode: 0o600 });
    }
    Object.assign(process.env, env);
    agent.on('log', line => console.log(`[agent] ${token ? line.replaceAll(token, '[redacted]') : line}`));
    const hello = event(agent, 'hello'), configured = event(agent, 'configured');
    agent.start();
    await Promise.all([hello, configured]);
    const pong = event(agent, 'pong'); agent.send({ type: 'ping' }); await pong;
    const devices = event(agent, 'devices'); agent.send({ type: 'devices.list' });
    const result = await devices;
    assert.equal(result.type, 'devices');
    if (result.type === 'devices') console.log(`Agent passed: hello, configure, ping, ${result.inputs.length} inputs and ${result.outputs.length} outputs.`);
    await agent.stop();
    const saved = JSON.parse(await readFile(join(configDirs[process.platform === 'darwin' ? 1 : 0], 'agent.json'), 'utf8'));
    assert.equal(saved.auto_inject, false);
    assert.equal(saved.correction_hotkey, null);
    console.log('Agent quit cleanly. Isolated test files removed; user config was not changed.');
  } finally {
    await agent.stop();
    for (const [key, value] of prior) {
      if (value === undefined) delete process.env[key]; else process.env[key] = value;
    }
    await rm(directory, { recursive: true, force: true });
  }
}

main().catch(error => { console.error(error instanceof Error ? error.message : error); process.exitCode = 1; });
