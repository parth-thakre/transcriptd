// Map guard edits and low-confidence words onto the final text.
// Port of client/src/renderer/annotations.ts (annotate + describeEdit); keep them in step.

// Mirrors server/text.py: marks remain significant, apostrophes remain inside words.
export function key(text) {
  return text.normalize('NFC').replace(/[’ʼ]/gu, "'").replace(/[\p{P}\p{S}]/gu, c => c === "'" ? c : '')
    .toLowerCase().replace(/ß/gu, 'ss').replace(/ς/gu, 'σ').replace(/़/gu, '').replace(/ँ/gu, 'ं').replace(/^'+|'+$/gu, '');
}

export function tokenize(text) {
  return Array.from(text.matchAll(/\S+/gu), m => ({ text: m[0], key: key(m[0]), start: m.index, end: m.index + m[0].length }));
}

/** Ordered edit-distance alignment. Bound memory for long pastes. */
function align(source, target) {
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
  const result = Array(target.length).fill(null);
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

/** Guard raw_span offsets are non-punctuation token indices; validate them against the raw text. */
function resolveEdit(raw, edit, index) {
  const [a, b] = edit.raw_span;
  const normalized = text => tokenize(text).map(t => t.key).filter(Boolean).join(' ');
  const needle = normalized(edit.raw);
  if (Number.isInteger(a) && Number.isInteger(b) && a >= 0 && b >= a && b <= raw.length
      && normalized(raw.slice(a, b).map(t => t.text).join(' ')) === needle) {
    return { edit, index, start: a, end: b, located: true };
  }
  const parts = tokenize(edit.raw).filter(t => t.key);
  const occurrences = raw.flatMap((_, start) => parts.length && parts.every((p, n) => raw[start + n]?.key === p.key) ? [start] : []);
  occurrences.sort((x, y) => Math.abs(x - a) - Math.abs(y - a));
  if (occurrences.length) return { edit, index, start: occurrences[0], end: occurrences[0] + parts.length, located: true };
  return { edit, index, start: 0, end: 0, located: false };
}

/** Each word of `finalText` with the edits that produced it, whether ASR was unsure of it,
 * and the raw words it came from (`heard`). */
export function annotate(result, finalText = result.text) {
  const raw = tokenize(result.raw_text).filter(t => t.key);
  const edits = (result.edits || []).map((e, n) => resolveEdit(raw, e, n));
  const expected = [];
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
  const asr = (result.words || []).flatMap((word, index) => tokenize(word.text).filter(t => t.key).map(t => ({ key: t.key, index })));
  const rawToAsr = align(asr.map(t => t.key), raw.map(t => t.key));
  const low = new Set((result.low_confidence || []).map(w => w.index));
  return final.map((token, n) => {
    const source = mapping[n] === null ? undefined : expected[mapping[n]];
    const sourceWords = source ? raw.slice(source.start, source.end) : [];
    const isLow = sourceWords.some((_, i) => {
      const mapped = rawToAsr[source.start + i];
      return mapped != null && low.has(asr[mapped].index);
    });
    return { ...token, edits: source?.edits ?? [], low: isLow, heard: sourceWords.map(t => t.text).join(' ') || token.text };
  });
}

export function tone(word) {
  const accepted = word.edits.map(e => e.edit).filter(e => e.accepted);
  if (accepted.some(e => e.reason === 'vocab')) return 'vocab';
  if (accepted.some(e => e.cleaned.trim() && e.reason !== 'filler')) return 'edit';
  return '';
}

export function describe(edit) {
  if (!edit.accepted) return 'Kept as heard: cleanup tried to change it';
  if (edit.reason === 'vocab') return 'Matched from your vocabulary';
  if (edit.reason === 'number') return 'Number written as digits';
  if (edit.reason === 'spelling variant') return 'Spelling normalized';
  return 'Changed by cleanup';
}
