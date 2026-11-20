import { strict as assert } from 'node:assert';
import { test } from 'node:test';
import { align, annotate, annotateRaw, describeEdit, key, resolveEdit, tokenize } from './annotations';
import type { CleanupEdit, DictationResult } from '../shared/types';

const edit = (raw: string, cleaned: string, raw_span: [number, number], accepted = true): CleanupEdit => ({ kind: cleaned ? 'replace' : 'delete', raw, cleaned, raw_span, accepted, reason: 'test' });
const result = (raw_text: string, text: string, edits: CleanupEdit[] = []): DictationResult => ({ raw_text, text, edits, replacements: [], low_confidence: [], words: tokenize(raw_text).map(word => ({ text: word.text, conf: .9, start_ms: 0, end_ms: 10 })) });

test('accepted token-span replacement carries the original raw evidence', () => {
  const value = annotate(result('use hyper land now', 'Use Hyprland now.', [edit('hyper land', 'Hyprland', [1, 3])]));
  assert.equal(value.words[1].heard, 'hyper land');
  assert.equal(value.words[1].edits.length, 1);
  assert.equal(value.words[2].heard, 'now');
  assert.equal(value.markers.length, 0);
});
test('accepted deletion is a marker before the surviving token', () => {
  const value = annotate(result('um hello there', 'Hello there.', [edit('um', '', [0, 1])]));
  assert.equal(value.markers.length, 1);
  assert.equal(value.markers[0].at, 0);
  assert.equal(value.words[0].heard, 'hello');
});
test('stutter alignment does not move confidence to the surviving repetition', () => {
  const source = result('we we go', 'We go.', [edit('we', '', [0, 1])]);
  source.low_confidence = [{ index: 0, text: 'we', conf: .3 }];
  const value = annotate(source);
  assert.equal(value.words[0].low, false);
  assert.equal(value.markers[0].at, 0);
});
test('ASR confidence aligns across many guard tokens inside one ASR word', () => {
  const source = result('hello hyper land now', 'Hello Hyprland now.', [edit('hyper land', 'Hyprland', [1, 3])]);
  source.words = [{ text: 'hello', conf: .9, start_ms: 0, end_ms: 10 }, { text: 'hyper land', conf: .2, start_ms: 10, end_ms: 20 }, { text: 'now', conf: .9, start_ms: 20, end_ms: 30 }];
  source.low_confidence = [{ index: 1, text: 'hyper land', conf: .2 }];
  const value = annotate(source);
  assert.equal(value.words[1].low, true);
  assert.equal(value.words[2].low, false);
});
test('legacy character spans are validated against raw evidence', () => {
  const source = result('use hyper land now', 'Use Hyprland now.', [edit('hyper land', 'Hyprland', [4, 14])]);
  assert.equal(annotate(source).words[1].heard, 'hyper land');
});
test('Python character spans preserve Unicode code-point offsets', () => {
  const raw = '🙂 नमस्ते हाइपर लैंड अब';
  const heard = 'हाइपर लैंड';
  const start = Array.from(raw.slice(0, raw.indexOf(heard))).length;
  const source = result(raw, 'नमस्ते Hyprland अब', [edit(heard, 'Hyprland', [start, start + Array.from(heard).length])]);
  assert.equal(annotate(source).words[1].heard, heard);
});
test('Indic combining marks remain in comparison keys', () => {
  assert.equal(key('नहीं।'), 'नहीं');
  assert.notEqual(key('की'), key('क')); 
  assert.equal(key('क़िताब'), key('किताब'));
  assert.equal(key('Straße'), key('STRASSE'));
});
test('guard indices exclude standalone punctuation', () => {
  const source = result('um , hello', 'Hello.', [edit('um', '', [0, 1])]);
  const value = annotate(source);
  assert.equal(value.words[0].heard, 'hello');
  assert.equal(value.markers[0].annotation.start, 0);
});
test('rejected edits never decorate final text as accepted', () => {
  const value = annotate(result('do not go', 'Do not go.', [edit('not', '', [1, 2], false)]));
  assert.equal(value.markers.length, 0);
  assert.ok(value.words.every(word => word.edits.length === 0));
  assert.equal(value.words[1].heard, 'not');
});
test('end deletions and whole-transcript deletions retain audit markers', () => {
  assert.equal(annotate(result('hello um', 'Hello.', [edit('um', '', [1, 2])])).markers[0].at, 1);
  assert.equal(annotate(result('um', '', [edit('um', '', [0, 1])])).markers[0].at, 0);
});
test('user final-text changes do not mutate raw transcript or edit audit', () => {
  const source = result('use hyper land', 'Use Hyprland.', [edit('hyper land', 'Hyprland', [1, 3])]);
  const before = JSON.stringify(source);
  annotate(source, 'Please use Hyprland tomorrow.');
  assert.equal(JSON.stringify(source), before);
});
test('an unlocatable accepted edit is retained as a marker', () => {
  const value = annotate(result('hello there', 'Hello there.', [edit('missing', 'new', [90, 100])]));
  assert.equal(value.markers.length, 1);
  assert.equal(value.markers[0].annotation.located, false);
});
test('repeated evidence fallback chooses the closest span', () => {
  const raw = 'one um two um three';
  const tokens = tokenize(raw);
  const found = resolveEdit(raw, tokens, edit('um', '', [4, 5]), 0);
  assert.equal(found.start, 3);
});
test('bounded long-text alignment is ordered and preserves unique anchors', () => {
  const source = Array.from({ length: 1100 }, (_, index) => `word${index}`);
  const target = ['inserted', ...source];
  const mapping = align(source, target);
  assert.equal(mapping[0], null);
  assert.equal(mapping[1], 0);
  assert.equal(mapping.at(-1), 1099);
});
test('raw evidence marks deleted fillers and keeps punctuation tokens unindexed', () => {
  const value = annotateRaw(result('um, hello there', 'Hello there.', [edit('um', '', [0, 1])]));
  assert.equal(value[0].text, 'um,');
  assert.equal(value[0].edits.length, 1);
  assert.equal(value[1].edits.length, 0);
  assert.deepEqual(value.map(t => t.index), [0, 1, 2]);
});
test('raw evidence carries low confidence from ASR words', () => {
  const source = result('ship the cooper netties', 'Ship the Kubernetes.', [edit('cooper netties', 'Kubernetes', [2, 4])]);
  source.low_confidence = [{ index: 2, text: 'cooper', conf: .3 }];
  const value = annotateRaw(source);
  assert.equal(value[2].low, true);
  assert.equal(value[3].low, false);
  assert.equal(value[3].edits[0].edit.cleaned, 'Kubernetes');
});
test('guard decisions map to user-facing tones', () => {
  assert.equal(describeEdit({ ...edit('cooper netties', 'Kubernetes', [0, 2]), reason: 'vocab' }).tone, 'vocab');
  assert.equal(describeEdit({ ...edit('um', '', [0, 1]), reason: 'filler' }).label, 'Filler removed');
  assert.equal(describeEdit({ ...edit('we', '', [0, 1]), reason: 'stutter' }).label, 'Repeated word removed');
  assert.equal(describeEdit({ ...edit('नाही', '', [0, 1], false), reason: 'protected word deleted' }).tone, 'kept');
});
