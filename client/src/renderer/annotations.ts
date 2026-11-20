import type { CleanupEdit, DictationResult } from '../shared/types';

export interface TextToken { text: string; key: string; start: number; end: number }
// Mirrors server/text.py: marks remain significant, apostrophes remain inside words.
export function key(text: string): string {
  return text.normalize('NFC').replace(/[’ʼ]/gu, "'").replace(/[\p{P}\p{S}]/gu, c => c === "'" ? c : '')
    .toLowerCase().replace(/ß/gu, 'ss').replace(/ς/gu, 'σ').replace(/़/gu, '').replace(/ँ/gu, 'ं').replace(/^'+|'+$/gu, '');
}
export function tokenize(text: string): TextToken[] {
  return Array.from(text.matchAll(/\S+/gu), m => ({ text: m[0], key: key(m[0]), start: m.index!, end: m.index! + m[0].length }));
}

/** Ordered edit-distance alignment. Bound memory for long meeting-sized pastes. */
export function align(source: string[], target: string[]): Array<number | null> {
  if (source.length * target.length > 1_000_000) {
    let cursor = 0;
    return target.map(value => {
      const index = source.indexOf(value, cursor);
      if (index < 0) return null;
      cursor = index + 1;
      return index;
    });
  }
  const width = target.length + 1;
  const costs = new Uint32Array((source.length + 1) * width);
  for (let i = 0; i <= source.length; i++) costs[i * width] = i;
  for (let j = 0; j <= target.length; j++) costs[j] = j;
  for (let i = 1; i <= source.length; i++) for (let j = 1; j <= target.length; j++) {
    costs[i * width + j] = Math.min(costs[(i - 1) * width + j] + 1, costs[i * width + j - 1] + 1,
      costs[(i - 1) * width + j - 1] + (source[i - 1] === target[j - 1] ? 0 : 1));
  }
  const result: Array<number | null> = Array(target.length).fill(null);
  let i = source.length, j = target.length;
  while (i || j) {
    const current = costs[i * width + j];
    if (i && j && current === costs[(i - 1) * width + j - 1] + (source[i - 1] === target[j - 1] ? 0 : 1)) {
      result[j - 1] = i - 1; i--; j--;
    } else if (i && current === costs[(i - 1) * width + j] + 1) i--;
    else j--;
  }
  return result;
}

export interface ResolvedEdit { edit: CleanupEdit; index: number; start: number; end: number; located: boolean }
/** Guard offsets are non-punctuation token indices. Older payloads may use Python
 * character offsets. Validate against raw evidence before accepting either form. */
export function resolveEdit(rawText: string, raw: TextToken[], edit: CleanupEdit, index: number): ResolvedEdit {
  const [a, b] = edit.raw_span;
  const normalized = (text: string) => tokenize(text).map(t => t.key).filter(Boolean).join(' ');
  const needle = normalized(edit.raw);
  const valid = Number.isInteger(a) && Number.isInteger(b) && a >= 0 && b >= a;
  if (valid && b <= raw.length && normalized(raw.slice(a, b).map(t => t.text).join(' ')) === needle) {
    return { edit, index, start: a, end: b, located: true };
  }
  if (valid) {
    const points = Array.from(rawText);
    const candidates: Array<[number, number]> = [[points.slice(0, a).join('').length, points.slice(0, b).join('').length], [a, b]];
    for (const [start, end] of candidates) {
      if (end <= rawText.length && normalized(rawText.slice(start, end)) === needle) {
        const first = raw.findIndex(t => t.end > start);
        const last = raw.findIndex(t => t.start >= end);
        return { edit, index, start: first < 0 ? raw.length : first, end: last < 0 ? raw.length : last, located: true };
      }
    }
  }
  // Evidence-first fallback, nearest occurrence to the supplied token hint.
  const parts = tokenize(edit.raw).filter(t => t.key);
  const occurrences = raw.flatMap((_, start) => parts.length && parts.every((p, n) => raw[start + n]?.key === p.key) ? [start] : []);
  occurrences.sort((x, y) => Math.abs(x - a) - Math.abs(y - a));
  if (occurrences.length) return { edit, index, start: occurrences[0], end: occurrences[0] + parts.length, located: true };
  return { edit, index, start: Math.max(0, Math.min(raw.length, Number.isFinite(a) ? a : 0)), end: Math.max(0, Math.min(raw.length, Number.isFinite(a) ? a : 0)), located: false };
}
export interface AnnotatedWord extends TextToken { edits: ResolvedEdit[]; low: boolean; heard: string }
export interface EditMarker { at: number; annotation: ResolvedEdit }
export function annotate(result: DictationResult, finalText = result.text): { words: AnnotatedWord[]; markers: EditMarker[] } {
  const raw = tokenize(result.raw_text).filter(t => t.key);
  const edits = result.edits.map((e, n) => resolveEdit(result.raw_text, raw, e, n));
  type Evidence = { key: string; start: number; end: number; edits: ResolvedEdit[] };
  const expected: Evidence[] = [];
  const accepted = edits.filter(e => e.edit.accepted && e.located).sort((a, b) => a.start - b.start || a.index - b.index);
  let cursor = 0;
  for (const edit of accepted) {
    if (edit.start < cursor) continue;
    while (cursor < edit.start) { expected.push({ key: raw[cursor].key, start: cursor, end: cursor + 1, edits: [] }); cursor++; }
    for (const token of tokenize(edit.edit.cleaned).filter(t => t.key)) expected.push({ key: token.key, start: edit.start, end: edit.end, edits: [edit] });
    cursor = edit.end;
  }
  while (cursor < raw.length) { expected.push({ key: raw[cursor].key, start: cursor, end: cursor + 1, edits: [] }); cursor++; }
  const final = tokenize(finalText);
  const mapping = align(expected.map(t => t.key), final.map(t => t.key));
  // ASR word indices are not guard token indices. Align them separately.
  const asr = result.words.flatMap((word, index) => tokenize(word.text).filter(t => t.key).map(t => ({ key: t.key, index })));
  const rawToAsr = align(asr.map(t => t.key), raw.map(t => t.key));
  const low = new Set(result.low_confidence.map(w => w.index));
  const words = final.map((token, n): AnnotatedWord => {
    const source = mapping[n] === null ? undefined : expected[mapping[n]!];
    const sourceWords = source ? raw.slice(source.start, source.end) : [];
    const isLow = sourceWords.some((_, i) => {
      const mapped = rawToAsr[source!.start + i];
      return mapped != null && low.has(asr[mapped].index);
    });
    return { ...token, edits: source?.edits ?? [], low: isLow, heard: sourceWords.map(t => t.text).join(' ') || token.text };
  });
  const marked = new Set(words.flatMap(w => w.edits.map(e => e.index)));
  const markers = edits.filter(e => e.edit.accepted && !marked.has(e.index)).map(annotation => {
    let at = final.length;
    for (let n = 0; n < mapping.length; n++) {
      const source = mapping[n] === null ? undefined : expected[mapping[n]!];
      if (source && source.start >= annotation.end) { at = n; break; }
    }
    return { at, annotation };
  });
  return { words, markers };
}

/** Raw evidence tokens (punctuation included) with the edits and confidence that touch them. */
export interface RawToken extends TextToken { index: number | null; edits: ResolvedEdit[]; low: boolean }
export function annotateRaw(result: DictationResult): RawToken[] {
  const all = tokenize(result.raw_text);
  const raw = all.filter(t => t.key);
  const edits = result.edits.map((e, n) => resolveEdit(result.raw_text, raw, e, n)).filter(e => e.located && e.end > e.start);
  const asr = result.words.flatMap((word, index) => tokenize(word.text).filter(t => t.key).map(t => ({ key: t.key, index })));
  const rawToAsr = align(asr.map(t => t.key), raw.map(t => t.key));
  const low = new Set(result.low_confidence.map(w => w.index));
  let cursor = 0;
  return all.map(token => {
    if (!token.key) return { ...token, index: null, edits: [], low: false };
    const index = cursor++;
    const mapped = rawToAsr[index];
    return { ...token, index, edits: edits.filter(e => e.start <= index && index < e.end), low: mapped != null && low.has(asr[mapped].index) };
  });
}

export type EditTone = 'vocab' | 'edit' | 'removed' | 'kept';
const keptReasons: Record<string, string> = {
  'protected word deleted': 'Removing it would drop a protected word',
  'protected word changed': 'Changing it would alter a protected word',
  'content word deleted': 'Removing it would drop a content word',
  'content word changed': 'Changing it would alter a content word',
  'word inserted': 'Cleanup tried to add a word',
};
/** Plain-language description of one guard decision. */
export function describeEdit(edit: CleanupEdit): { tone: EditTone; label: string } {
  if (!edit.accepted) return { tone: 'kept', label: keptReasons[edit.reason] ?? (edit.reason ? `Rejected: ${edit.reason}` : 'Rejected by the guard') };
  if (edit.reason === 'vocab') return { tone: 'vocab', label: 'Vocabulary match' };
  if (!edit.cleaned.trim()) return { tone: 'removed', label: edit.reason === 'stutter' ? 'Repeated word removed' : edit.reason === 'filler' ? 'Filler removed' : 'Removed' };
  return { tone: 'edit', label: edit.reason === 'spelling variant' ? 'Spelling normalized' : edit.reason === 'filler' ? 'Filler removed' : 'Changed by cleanup' };
}
