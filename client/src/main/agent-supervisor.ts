import { spawn, type ChildProcessWithoutNullStreams } from 'node:child_process';
import { EventEmitter } from 'node:events';
import { join, resolve } from 'node:path';
import type { AgentCommand, AgentConfig, AgentEvent, AgentStatus } from '../shared/types.ts';

export interface AgentSupervisorOptions {
  getConfig: () => AgentConfig;
  appRoot: string;
  resourcesPath: string;
  packaged: boolean;
}

type SupervisorEvents = { event: [AgentEvent]; status: [AgentStatus]; log: [string] };
const MAX_LINE = 1024 * 1024;

export class AgentSupervisor extends EventEmitter<SupervisorEvents> {
  private child: ChildProcessWithoutNullStreams | null = null;
  private wanted = false;
  private status: AgentStatus = 'offline';
  private retries = 0;
  private retryTimer: ReturnType<typeof setTimeout> | undefined;
  private stopping: Promise<void> | undefined;
  private revision = 0;

  private readonly options: AgentSupervisorOptions;
  constructor(options: AgentSupervisorOptions) { super(); this.options = options; }

  start(): void {
    this.wanted = true;
    this.revision++;
    if (this.stopping) {
      void this.stopping.then(() => { if (this.wanted) this.launch(); });
      return;
    }
    this.launch();
  }

  stop(): Promise<void> {
    this.wanted = false;
    this.revision++;
    clearTimeout(this.retryTimer);
    this.retryTimer = undefined;
    this.retries = 0;
    if (this.stopping) return this.stopping;
    const child = this.child;
    if (!child) { this.setStatus('offline'); return Promise.resolve(); }

    this.stopping = new Promise<void>(resolveStop => {
      // Let the agent finish its current command, but never hang application exit.
      const terminate = setTimeout(() => { child.kill('SIGTERM'); }, 2_000);
      const kill = setTimeout(() => { child.kill('SIGKILL'); }, 3_000);
      child.once('close', () => {
        clearTimeout(terminate);
        clearTimeout(kill);
        resolveStop();
      });
      if (child.stdin.writable && !child.stdin.destroyed) {
        child.stdin.end(`${JSON.stringify({ type: 'quit' })}\n`);
      } else {
        child.kill('SIGTERM');
      }
    }).finally(() => {
      this.stopping = undefined;
      this.setStatus('offline');
    });
    return this.stopping;
  }

  async restart(): Promise<void> {
    // Rust saves configure synchronously, then processes quit in stdin order.
    // Persist hotkey settings before the next process binds its initial hotkeys.
    if (this.child && this.wanted && !this.stopping && this.child.stdin.writable && !this.child.stdin.destroyed) {
      this.send({ type: 'configure', config: this.options.getConfig() });
    }
    const stopped = this.stop();
    const revision = this.revision;
    await stopped;
    // A later explicit stop/start takes precedence over this restart request.
    if (revision === this.revision) this.start();
  }

  send(command: AgentCommand): void {
    const child = this.child;
    if (!child || !this.wanted || this.stopping || child.stdin.destroyed || !child.stdin.writable) {
      throw new Error('The agent is not running.');
    }
    if (child.stdin.writableLength > MAX_LINE) throw new Error('The agent command queue is full.');
    child.stdin.write(`${JSON.stringify(command)}\n`, error => {
      if (error) this.emit('log', `Agent command failed: ${error.message}`);
    });
  }

  private setStatus(status: AgentStatus): void {
    if (this.status !== status) { this.status = status; this.emit('status', status); }
  }

  private launch(): void {
    if (!this.wanted || this.child || this.retryTimer || this.stopping) return;
    this.setStatus(this.retries ? 'restarting' : 'starting');
    let config: AgentConfig;
    let child: ChildProcessWithoutNullStreams;
    try {
      config = this.options.getConfig();
      const name = process.platform === 'win32' ? 'transcriptd-agent.exe' : 'transcriptd-agent';
      const binary = process.env.TRANSCRIPTD_AGENT || (this.options.packaged
        ? join(this.options.resourcesPath, 'bin', name)
        : resolve(this.options.appRoot, '..', 'agent', 'target', 'debug', name));
      const args = ['run', '--hotkey', config.hotkey, '--mode', config.hotkey_mode];
      if (!config.auto_inject) args.push('--no-inject');
      child = spawn(binary, args, {
        // An ASAR archive is not a real working directory for a native child.
        cwd: this.options.packaged ? this.options.resourcesPath : this.options.appRoot, env: { ...process.env },
        stdio: ['pipe', 'pipe', 'pipe'], windowsHide: true,
      });
    } catch (error) {
      this.emit('log', `Could not start agent: ${message(error)}`);
      this.scheduleRestart();
      return;
    }
    this.child = child;
    const startedAt = Date.now();
    child.stdin.on('error', error => this.emit('log', `Agent stdin: ${error.message}`));
    child.on('error', error => this.emit('log', `Could not run agent: ${error.message}`));
    child.once('spawn', () => {
      if (!this.wanted || this.stopping) return;
      // Each new process must receive the current settings, including respawns.
      this.send({ type: 'configure', config });
    });
    this.lines(child.stdout, line => {
      let event: unknown;
      try { event = JSON.parse(line); }
      catch { this.emit('log', 'Ignored malformed agent JSON.'); return; }
      if (!isAgentEvent(event)) { this.emit('log', 'Ignored unsupported or malformed agent event.'); return; }
      if (this.child !== child || this.stopping || !this.wanted) return;
      if (event.type === 'configured') this.setStatus('online');
      this.emit('event', event);
    });
    this.lines(child.stderr, line => this.emit('log', line));
    child.once('close', (code, signal) => {
      if (this.child !== child) return;
      this.child = null;
      if (!this.wanted || this.stopping) return;
      this.emit('log', `Agent exited (${signal ?? code ?? 'unknown'}).`);
      if (Date.now() - startedAt > 30_000) this.retries = 0;
      this.scheduleRestart();
    });
  }

  private scheduleRestart(): void {
    if (!this.wanted || this.retryTimer) return;
    this.setStatus('restarting');
    const delay = Math.min(500 * 2 ** Math.min(this.retries++, 6), 30_000);
    this.emit('log', `Restarting agent in ${delay} ms.`);
    this.retryTimer = setTimeout(() => {
      this.retryTimer = undefined;
      this.launch();
    }, delay);
  }

  private lines(stream: NodeJS.ReadableStream & { setEncoding(encoding: BufferEncoding): unknown }, onLine: (line: string) => void): void {
    stream.setEncoding('utf8');
    let pending = '';
    let discarding = false;
    stream.on('data', (chunk: string) => {
      for (const [index, part] of chunk.split('\n').entries()) {
        if (index > 0) {
          if (!discarding && pending.trim()) onLine(pending.replace(/\r$/, ''));
          pending = '';
          discarding = false;
        }
        if (!discarding) {
          pending += part;
          if (pending.length > MAX_LINE) {
            pending = '';
            discarding = true;
            this.emit('log', 'Ignored oversized agent output line.');
          }
        }
      }
    });
    stream.on('end', () => { if (!discarding && pending.trim()) onLine(pending.replace(/\r$/, '')); });
    stream.on('error', error => this.emit('log', `Agent output: ${message(error)}`));
  }
}

function message(error: unknown): string { return error instanceof Error ? error.message : String(error); }
function record(value: unknown): value is Record<string, unknown> { return !!value && typeof value === 'object' && !Array.isArray(value); }
function isAgentEvent(value: unknown): value is AgentEvent {
  if (!record(value)) return false;
  const string = (key: string) => typeof value[key] === 'string';
  const number = (key: string) => typeof value[key] === 'number' && Number.isFinite(value[key]);
  switch (value.type) {
    case 'hello': return ['version', 'platform', 'hotkey_backend', 'inject_backend'].every(string);
    case 'configured': return Array.isArray(value.warnings) && value.warnings.every(v => typeof v === 'string');
    case 'ptt.down': return string('source');
    case 'ptt.up': case 'pong': return true;
    case 'level': return number('db');
    case 'partial': return string('text');
    case 'final': return record(value.result) && typeof value.result.text === 'string' && typeof value.injected === 'boolean';
    case 'injected': return number('chars') && string('method');
    case 'corrections': return (value.dictation_id == null || number('dictation_id')) && string('app')
      && Array.isArray(value.corrections) && value.corrections.every(c => record(c) && typeof c.heard === 'string' && typeof c.meant === 'string');
    case 'clipboard': case 'selection': return value.text == null || string('text');
    case 'devices': return [value.inputs, value.outputs].every(list => Array.isArray(list) && list.every(item =>
      record(item) && typeof item.name === 'string' && typeof item.default === 'boolean'));
    case 'meeting.started': return string('wav_path');
    case 'meeting.level': return number('me_db') && number('them_db');
    case 'meeting.segment': return record(value.segment) && typeof value.segment.text === 'string'
      && typeof value.segment.channel === 'string' && typeof value.segment.start_ms === 'number' && typeof value.segment.end_ms === 'number';
    case 'meeting.stopped': return string('wav_path') && number('duration_ms');
    case 'error': return string('message') && typeof value.fatal === 'boolean';
    default: return false;
  }
}
