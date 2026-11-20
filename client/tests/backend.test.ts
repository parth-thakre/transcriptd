import { afterEach, describe, expect, test } from 'bun:test';
import { createServer } from 'node:http';
import { mkdtemp, readFile, rm, writeFile } from 'node:fs/promises';
import { fileURLToPath } from 'node:url';
import { join } from 'node:path';
import { AgentSupervisor } from '../src/main/agent-supervisor.ts';
import { findCorrections } from '../src/main/corrections.ts';
import { HttpServerApi } from '../src/main/server-api.ts';
import type { AgentConfig, AgentEvent, AgentStatus } from '../src/shared/types.ts';

const appRoot = fileURLToPath(new URL('..', import.meta.url));
const config: AgentConfig = {
  server_url: 'http://127.0.0.1:7411', token: 'test-token', profile: null, language: null,
  hotkey: 'ctrl+alt+f24', correction_hotkey: null, hotkey_mode: 'hold', auto_inject: false,
  inject: { method: 'clipboard', paste_chord: null, restore_clipboard: true, restore_delay_ms: 400, trailing_space: false },
  mic_device: null, loopback_device: null, partials: false,
};
const cleanup: Array<() => Promise<void>> = [];
afterEach(async () => { while (cleanup.length) await cleanup.pop()!(); });

async function httpFixture(reply: (url: string) => { status?: number; body?: unknown; headers?: Record<string, string> }) {
  const requests: Array<{ method: string; url: string; token: string | undefined; body: unknown }> = [];
  const server = createServer(async (request, response) => {
    let body = '';
    for await (const chunk of request) body += chunk;
    requests.push({ method: request.method!, url: request.url!, token: request.headers.authorization, body: body ? JSON.parse(body) : undefined });
    const result = reply(request.url!);
    response.writeHead(result.status ?? 200, result.headers);
    response.end(typeof result.body === 'string' ? result.body : result.body === undefined ? undefined : JSON.stringify(result.body));
  });
  await new Promise<void>(resolve => server.listen(0, '127.0.0.1', resolve));
  cleanup.push(() => new Promise<void>((resolve, reject) => server.close(error => error ? reject(error) : resolve())));
  const address = server.address();
  if (!address || typeof address === 'string') throw new Error('No test server address');
  return { serverUrl: `http://127.0.0.1:${address.port}`, requests };
}

describe('HttpServerApi', () => {
  test('maps every ServerApi endpoint, method, body, and text/204 response', async () => {
    const fixture = await httpFixture(url => url.endsWith('/export') || url.includes('/transcript?')
      ? { body: 'plain text\n' } : url.includes('/suppress') || url.endsWith('/99') ? { status: 204 } : { body: { ok: true } });
    const api = new HttpServerApi(() => ({ serverUrl: fixture.serverUrl + '/', token: 'secret' }));
    const vocab = { term: 'kubectl', aliases: ['kube control'], heard_as: ['cube cuddle'] };
    const correction = { heard: 'cube', meant: 'kube', dictation_id: 4, source: 'test' };
    const cases: Array<[() => Promise<unknown>, string, string, unknown?]> = [
      [() => api.health(), 'GET', '/v1/health'],
      [() => api.dictations('a & b/é'), 'GET', '/v1/dictations?q=a+%26+b%2F%C3%A9'],
      [() => api.dictation(4), 'GET', '/v1/dictations/4'],
      [() => api.setFinal(4, 'edited'), 'POST', '/v1/dictations/4/final', { final_text: 'edited' }],
      [() => api.vocab(), 'GET', '/v1/vocab'],
      [() => api.addVocab(vocab), 'POST', '/v1/vocab', vocab],
      [() => api.updateVocab(7, vocab), 'PUT', '/v1/vocab/7', vocab],
      [() => api.deleteVocab(99), 'DELETE', '/v1/vocab/99'],
      [() => api.importVocab('term | alias'), 'POST', '/v1/vocab/import', { text: 'term | alias' }],
      [() => api.exportVocab(), 'GET', '/v1/vocab/export'],
      [() => api.suppress('word'), 'POST', '/v1/vocab/suppress', { word: 'word' }],
      [() => api.correction(correction), 'POST', '/v1/corrections', correction],
      [() => api.meetings('one two'), 'GET', '/v1/meetings?q=one+two'],
      [() => api.createMeeting({ title: 'Test', profile: null }), 'POST', '/v1/meetings', { title: 'Test', profile: null }],
      [() => api.meeting(3), 'GET', '/v1/meetings/3'],
      [() => api.patchMeeting(3, { notes: 'notes' }), 'PATCH', '/v1/meetings/3', { notes: 'notes' }],
      [() => api.deleteMeeting(99), 'DELETE', '/v1/meetings/99'],
      [() => api.transcript(3, 'md'), 'GET', '/v1/meetings/3/transcript?fmt=md'],
      [() => api.transcript(3), 'GET', '/v1/meetings/3/transcript?fmt=txt'],
      [() => api.summarize(3), 'POST', '/v1/meetings/3/summarize'],
      [() => api.processMeeting(3), 'POST', '/v1/meetings/3/process'],
    ];
    for (const [call, method, url, body] of cases) {
      const result = await call();
      expect(fixture.requests.at(-1)).toEqual({ method, url, body, token: 'Bearer secret' });
      if (url.includes('/transcript?') || url.endsWith('/export')) expect(result).toBe('plain text\n');
      else if (url.endsWith('/99') || url.endsWith('/suppress')) expect(result).toBeUndefined();
      else expect(result).toEqual({ ok: true });
    }
  });

  test('uses fresh connection settings and permits a health-check override', async () => {
    const fixture = await httpFixture(() => ({ body: { ok: true } }));
    let token = 'first';
    const api = new HttpServerApi(() => ({ serverUrl: fixture.serverUrl, token }));
    await api.health(); token = 'second'; await api.health();
    await api.health({ serverUrl: fixture.serverUrl, token: 'override' });
    expect(fixture.requests.map(r => r.token)).toEqual(['Bearer first', 'Bearer second', 'Bearer override']);
  });

  test('surfaces useful HTTP errors, validates URLs, and refuses redirects', async () => {
    const fixture = await httpFixture(url => url.includes('vocab')
      ? { status: 302, headers: { Location: '/v1/health' } }
      : { status: 401, body: { detail: 'invalid or missing token' } });
    const api = new HttpServerApi(() => ({ serverUrl: fixture.serverUrl, token: '' }));
    await expect(api.health()).rejects.toThrow('401: invalid or missing token');
    await expect(api.vocab()).rejects.toThrow('could not connect');
    expect(fixture.requests).toHaveLength(2);
    for (const serverUrl of ['file:///tmp/server', 'http://user:pass@localhost', 'http://localhost/?token=foo']) {
      await expect(api.health({ serverUrl, token: '' })).rejects.toThrow('Server URL must');
    }
  });
});

describe('findCorrections', () => {
  const recent = [{ id: 42, text: 'Please use cube cuddle for the deployment today.' }];
  test('learns phrase substitutions against the closest recent dictation', () => {
    const result = findCorrections('Please use kubectl for the deployment today.', [
      { id: 50, text: 'Please use the meeting room today' }, ...recent,
    ]);
    expect(result).toEqual([{ heard: 'cube cuddle', meant: 'kubectl', dictation_id: 42, source: 'correction_hotkey' }]);
  });
  test('ignores punctuation, casing, pure insertions and deletions', () => {
    expect(findCorrections('PLEASE use cube cuddle, for the deployment today!', recent)).toEqual([]);
    expect(findCorrections('Please use cube cuddle for the big deployment today.', recent)).toEqual([]);
    expect(findCorrections('Please use cube cuddle for deployment today.', recent)).toEqual([]);
  });
  test('rejects unrelated, short, and oversized selections', () => {
    for (const text of ['', 'kubectl', 'Please deploy tomorrow', 'A completely unrelated sentence about lunch', 'word '.repeat(401)]) {
      expect(findCorrections(text, recent)).toEqual([]);
    }
  });
  test('preserves Hindi and Marathi vowel marks in learned words', () => {
    expect(findCorrections('आज मला पुण्याला जावे लागेल', [{id: 8, text: 'आज मला मुंबईला जावे लागेल'}])).toEqual([
      {heard: 'मुंबईला', meant: 'पुण्याला', dictation_id: 8, source: 'correction_hotkey'},
    ]);
  });
  test('prefers an exact candidate over learning a false correction', () => {
    expect(findCorrections('Please use kubectl for the deployment today', [
      ...recent, { id: 4, text: 'Please use kubectl for the deployment today' },
    ])).toEqual([]);
  });
  test('extracts separate substitutions, Unicode words, and null IDs', () => {
    expect(findCorrections('Please ask Zoë to bring café tomorrow', [
      { id: null, text: 'Please ask Zoe to bring coffee tomorrow' },
    ])).toEqual([
      { heard: 'Zoe', meant: 'Zoë', dictation_id: null, source: 'correction_hotkey' },
      { heard: 'coffee', meant: 'café', dictation_id: null, source: 'correction_hotkey' },
    ]);
  });
});

function waitEvent(supervisor: AgentSupervisor, type: AgentEvent['type'], timeout = 4_000): Promise<AgentEvent> {
  return new Promise((resolve, reject) => {
    const timer = setTimeout(() => { supervisor.off('event', listener); reject(new Error(`Timed out waiting for ${type}`)); }, timeout);
    const listener = (event: AgentEvent) => {
      if (event.type !== type) return;
      clearTimeout(timer); supervisor.off('event', listener); resolve(event);
    };
    supervisor.on('event', listener);
  });
}

async function agentFixture(extra = '') {
  const dir = await mkdtemp(join(appRoot, 'tests', '.backend-'));
  const binary = join(dir, 'agent.cjs');
  await writeFile(binary, `#!/usr/bin/env node
const fs = require('node:fs');
const send = event => process.stdout.write(JSON.stringify(event) + '\\n');
const rl = require('node:readline').createInterface({input: process.stdin});
${extra}
process.stdout.write('{"type":"hello","version":"0.1","platform":"test",');
setTimeout(() => process.stdout.write('"hotkey_backend":"none","inject_backend":"none"}\\n'), 10);
rl.on('line', line => {
  const command = JSON.parse(line);
  fs.appendFileSync(require('node:path').join(__dirname, 'commands.jsonl'), line + '\\n');
  if (command.type === 'configure') {
    setTimeout(() => {
      process.stderr.write('fixture stderr\\n');
      process.stdout.write('not json\\n{"type":"level","db":"bad"}\\n');
      send({type:'configured', warnings:[command.config.token]});
    }, 20);
  }
  if (command.type === 'ping') {
    send({type:'meeting.segment',segment:{id:4,channel:'me',speaker:'me',start_ms:0,end_ms:100,text:'hello',live:true}});
    send({type:'corrections',dictation_id:7,app:'editor',corrections:[{heard:'mark',meant:'Marc'}]});
    send({type:'corrections',dictation_id:7,app:'editor',corrections:[{heard:1}]});
    send({type:'ptt.up'}); send({type:'pong'});
  }
  if (command.type === 'quit') process.exit(0);
});
`, { mode: 0o700 });
  const prior = process.env.TRANSCRIPTD_AGENT;
  process.env.TRANSCRIPTD_AGENT = binary;
  cleanup.push(async () => {
    if (prior === undefined) delete process.env.TRANSCRIPTD_AGENT; else process.env.TRANSCRIPTD_AGENT = prior;
    await rm(dir, { recursive: true, force: true });
  });
  return dir;
}

function supervisor(getConfig = () => config) {
  const agent = new AgentSupervisor({ getConfig, appRoot, resourcesPath: appRoot, packaged: false });
  cleanup.push(() => agent.stop());
  return agent;
}

describe('AgentSupervisor', () => {
  test('configures, parses chunked events, logs stderr, and stops without respawning', async () => {
    await agentFixture();
    const agent = supervisor();
    const statuses: AgentStatus[] = [], logs: string[] = [], events: AgentEvent[] = [];
    agent.on('status', status => statuses.push(status)); agent.on('log', line => logs.push(line)); agent.on('event', event => events.push(event));
    const configured = waitEvent(agent, 'configured'); agent.start(); await configured;
    expect(events.some(event => event.type === 'hello')).toBe(true);
    const pong = waitEvent(agent, 'pong'); agent.send({ type: 'ping' }); await pong;
    expect(events.some(event => event.type === 'meeting.segment')).toBe(true);
    expect(events.some(event => event.type === 'ptt.up')).toBe(true);
    expect(events.filter(event => event.type === 'corrections')).toEqual([
      { type: 'corrections', dictation_id: 7, app: 'editor', corrections: [{ heard: 'mark', meant: 'Marc' }] }]);
    expect(events.some(event => event.type === 'level')).toBe(false);
    expect(logs).toContain('fixture stderr');
    expect(logs).toContain('Ignored malformed agent JSON.');
    await agent.stop();
    expect(statuses).toEqual(['starting', 'online', 'offline']);
    expect(() => agent.send({ type: 'ping' })).toThrow('not running');
  });

  test('restart reconfigures with fresh settings and start is idempotent', async () => {
    const dir = await agentFixture();
    let token = 'one';
    const agent = supervisor(() => ({ ...config, token }));
    let configured = waitEvent(agent, 'configured'); agent.start(); agent.start();
    expect(await configured).toMatchObject({ warnings: ['one'] });
    token = 'two'; configured = waitEvent(agent, 'configured'); await agent.restart();
    expect(await configured).toMatchObject({ warnings: ['two'] });
    const commands = (await readFile(join(dir, 'commands.jsonl'), 'utf8')).trim().split('\n').map(line => JSON.parse(line));
    expect(commands.map(command => [command.type, command.config?.token])).toEqual([
      ['configure', 'one'], ['configure', 'two'], ['quit', undefined], ['configure', 'two'],
    ]);
    await Promise.all([agent.stop(), agent.stop()]);
  });

  test('unexpected exits use backoff and stop cancels a pending retry', async () => {
    await agentFixture('setTimeout(() => process.exit(17), 80);');
    const agent = supervisor();
    const delays: string[] = [];
    const restarted = new Promise<void>((resolve, reject) => {
      const timeout = setTimeout(() => reject(new Error('No backoff')), 4_000);
      agent.on('log', line => {
        if (line.startsWith('Restarting agent in')) delays.push(line);
        if (delays.length === 2) { clearTimeout(timeout); resolve(); }
      });
    });
    agent.start(); await restarted; await agent.stop();
    expect(delays).toEqual(['Restarting agent in 500 ms.', 'Restarting agent in 1000 ms.']);
  });

  test('packaged agent uses resources rather than an ASAR working directory', async () => {
    const dir = await agentFixture();
    const agent = new AgentSupervisor({ getConfig: () => config, appRoot: join(dir, 'app.asar'), resourcesPath: dir, packaged: true });
    cleanup.push(() => agent.stop());
    const configured = waitEvent(agent, 'configured');
    agent.start();
    await configured;
    await agent.stop();
  });

  test('missing binary reports the failure without unhandled error events', async () => {
    const dir = await agentFixture(); process.env.TRANSCRIPTD_AGENT = join(dir, 'missing');
    const agent = supervisor();
    const failure = new Promise<void>(resolve => agent.on('log', line => { if (line.startsWith('Restarting agent in')) resolve(); }));
    agent.start(); await failure; await agent.stop();
  });
});
