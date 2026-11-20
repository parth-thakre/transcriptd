import type {
  Correction, CorrectionReply, DictationDetail, DictationRow, Health, Meeting,
  ServerApi, VocabInput, VocabTerm,
} from '../shared/types.ts';

type Connection = { serverUrl: string; token: string };

/** REST client shared by Electron's main process and headless tests. */
export class HttpServerApi implements ServerApi {
  private readonly getConnection: () => Connection;
  constructor(getConnection: () => Connection) { this.getConnection = getConnection; }

  private async request<T>(
    path: string,
    options: { method?: string; body?: unknown; response?: 'json' | 'text' | 'empty'; connection?: Connection; timeout?: number } = {},
  ): Promise<T> {
    const { serverUrl, token } = options.connection ?? this.getConnection();
    const base = new URL(serverUrl.trim());
    if (!['http:', 'https:'].includes(base.protocol) || base.username || base.password || base.search || base.hash) {
      throw new Error('Server URL must be an HTTP(S) URL without credentials, a query, or a fragment.');
    }
    const url = new URL(`${base.href.replace(/\/+$/, '')}/v1${path}`);
    const headers: Record<string, string> = { Accept: options.response === 'text' ? 'text/plain' : 'application/json' };
    if (token.trim()) headers.Authorization = `Bearer ${token.trim()}`;
    if (options.body !== undefined) headers['Content-Type'] = 'application/json';
    let response: Response;
    try {
      response = await fetch(url, {
        method: options.method ?? 'GET', headers,
        body: options.body === undefined ? undefined : JSON.stringify(options.body),
        signal: AbortSignal.timeout(options.timeout ?? 30_000),
        // Do not send the bearer token to a redirected endpoint.
        redirect: 'error',
      });
    } catch (error) {
      const reason = error instanceof Error && (error.name === 'TimeoutError' || error.name === 'AbortError')
        ? 'request timed out' : 'could not connect';
      throw new Error(`Server ${reason}. Check the server URL and network connection.`, { cause: error });
    }
    if (!response.ok) {
      const body = await response.text();
      let detail = body;
      try {
        const parsed = JSON.parse(body) as { detail?: unknown };
        detail = typeof parsed.detail === 'string' ? parsed.detail : JSON.stringify(parsed.detail ?? parsed);
      } catch { /* Plain-text HTTP error. */ }
      // A broken server must not turn an error dialog into a megabyte of HTML.
      throw new Error(`Server returned ${response.status}${detail ? `: ${detail.slice(0, 600)}` : ` ${response.statusText}`}`);
    }
    if (options.response === 'empty' || response.status === 204) return undefined as T;
    if (options.response === 'text') return await response.text() as T;
    return await response.json() as T;
  }

  health(connection?: Connection): Promise<Health> { return this.request('/health', { connection }); }
  dictations(q?: string): Promise<DictationRow[]> { return this.request(`/dictations${query(q)}`); }
  dictation(id: number): Promise<DictationDetail> { return this.request(`/dictations/${id}`); }
  setFinal(id: number, finalText: string): Promise<{ ok: boolean }> {
    return this.request(`/dictations/${id}/final`, { method: 'POST', body: { final_text: finalText } });
  }
  vocab(): Promise<VocabTerm[]> { return this.request('/vocab'); }
  addVocab(input: VocabInput): Promise<VocabTerm> { return this.request('/vocab', { method: 'POST', body: input }); }
  updateVocab(id: number, input: VocabInput): Promise<VocabTerm> {
    return this.request(`/vocab/${id}`, { method: 'PUT', body: input });
  }
  deleteVocab(id: number): Promise<void> { return this.request(`/vocab/${id}`, { method: 'DELETE', response: 'empty' }); }
  importVocab(text: string): Promise<{ added: number }> { return this.request('/vocab/import', { method: 'POST', body: { text } }); }
  exportVocab(): Promise<string> { return this.request('/vocab/export', { response: 'text' }); }
  suppress(word: string): Promise<void> { return this.request('/vocab/suppress', { method: 'POST', body: { word }, response: 'empty' }); }
  correction(input: Correction): Promise<CorrectionReply> { return this.request('/corrections', { method: 'POST', body: input }); }
  meetings(q?: string): Promise<Meeting[]> { return this.request(`/meetings${query(q)}`); }
  createMeeting(input: { title: string; profile?: string | null }): Promise<Meeting> {
    return this.request('/meetings', { method: 'POST', body: input });
  }
  meeting(id: number): Promise<Meeting> { return this.request(`/meetings/${id}`); }
  patchMeeting(id: number, input: { title?: string; notes?: string }): Promise<Meeting> {
    return this.request(`/meetings/${id}`, { method: 'PATCH', body: input });
  }
  deleteMeeting(id: number): Promise<void> { return this.request(`/meetings/${id}`, { method: 'DELETE', response: 'empty' }); }
  transcript(id: number, format: 'md' | 'txt' = 'txt'): Promise<string> {
    return this.request(`/meetings/${id}/transcript?fmt=${format}`, { response: 'text' });
  }
  summarize(id: number): Promise<{ summary: string }> {
    return this.request(`/meetings/${id}/summarize`, { method: 'POST', timeout: 180_000 });
  }
  processMeeting(id: number): Promise<{ ok: boolean }> { return this.request(`/meetings/${id}/process`, { method: 'POST' }); }
}

function query(q?: string): string { return q ? `?${new URLSearchParams({ q })}` : ''; }
