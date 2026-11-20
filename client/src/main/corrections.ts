import type { Correction } from '../shared/types.ts';

type Token = { text: string; key: string };
const MAX_WORDS = 400;

function words(text: string): Token[] {
  return Array.from(text.matchAll(/[\p{L}\p{N}][\p{L}\p{M}\p{N}\u200c\u200d]*(?:['’\-][\p{L}\p{N}][\p{L}\p{M}\p{N}\u200c\u200d]*)*/gu), match => ({
    text: match[0], key: match[0].normalize('NFKC').toLowerCase().replaceAll('’', "'"),
  }));
}

/** Returns ordered matching word indices, ignoring punctuation and capitalization. */
function lcs(a: Token[], b: Token[]): Array<[number, number]> {
  const width = b.length + 1;
  const lengths = new Uint16Array((a.length + 1) * width);
  for (let i = a.length - 1; i >= 0; i--) {
    for (let j = b.length - 1; j >= 0; j--) {
      lengths[i * width + j] = a[i].key === b[j].key
        ? 1 + lengths[(i + 1) * width + j + 1]
        : Math.max(lengths[(i + 1) * width + j], lengths[i * width + j + 1]);
    }
  }
  const matches: Array<[number, number]> = [];
  let i = 0, j = 0;
  while (i < a.length && j < b.length) {
    if (a[i].key === b[j].key) { matches.push([i++, j++]); }
    else if (lengths[(i + 1) * width + j] >= lengths[i * width + j + 1]) i++;
    else j++;
  }
  return matches;
}

/** Learn small substitutions, never pure additions/deletions or unrelated selections.
 * Pass recent dictations newest first; that order breaks ties between equal matches.
 */
export function findCorrections(selection: string, recent: Array<{ id: number | null; text: string }>): Correction[] {
  const edited = words(selection);
  if (edited.length < 3 || edited.length > MAX_WORDS) return [];
  let best: { id: number | null; original: Token[]; matches: Array<[number, number]>; score: number } | undefined;
  for (const candidate of recent.slice(0, 100)) {
    const original = words(candidate.text);
    if (original.length < 3 || original.length > MAX_WORDS) continue;
    const matches = lcs(original, edited);
    const score = matches.length / Math.max(original.length, edited.length);
    // Two shared words and 60% overlap keep short or unrelated text out of learning.
    if (matches.length < 2 || score < 0.6 || (best && score <= best.score)) continue;
    best = { id: candidate.id, original, matches, score };
  }
  if (!best) return [];
  const corrections: Correction[] = [];
  let oldStart = 0, newStart = 0;
  for (const [oldEnd, newEnd] of [...best.matches, [best.original.length, edited.length]]) {
    const heard = best.original.slice(oldStart, oldEnd);
    const meant = edited.slice(newStart, newEnd);
    // Long rewrites are not reliable vocabulary evidence. A phrase can still
    // replace one word (or vice versa), e.g. "cube cuddle" -> "kubectl".
    if (heard.length && meant.length && heard.length <= 6 && meant.length <= 6) {
      corrections.push({
        heard: heard.map(word => word.text).join(' '),
        meant: meant.map(word => word.text).join(' '),
        dictation_id: best.id,
        source: 'correction_hotkey',
      });
    }
    oldStart = oldEnd + 1;
    newStart = newEnd + 1;
  }
  return corrections;
}
