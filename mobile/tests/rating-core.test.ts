import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { test } from 'node:test';

import { ASK_GAP_MS, EMPTY_STATE, isGood, onFreshResult, parseState } from '../src/lib/rating-core.ts';

const NOW = Date.UTC(2026, 10, 14);

test('good means 7.0+ out of 10 on an available score', () => {
  assert.equal(isGood(7, 10, true), true);
  assert.equal(isGood(6.9, 10, true), false);
  assert.equal(isGood(9, 10, false), false);
  assert.equal(isGood(null, 10, true), false);
});

test('never on the first good result; asks on the second', () => {
  const first = onFreshResult(EMPTY_STATE, true, NOW);
  assert.equal(first.ask, false);
  const second = onFreshResult(first.state, true, NOW + 1000);
  assert.equal(second.ask, true);
  assert.equal(second.state.lastAskedMs, NOW + 1000);
});

test('a poor result never asks and does not count', () => {
  const r = onFreshResult({ goodResults: 5, lastAskedMs: null }, false, NOW);
  assert.equal(r.ask, false);
  assert.equal(r.state.goodResults, 5);
});

test('at most once every 120 days', () => {
  const asked = { goodResults: 3, lastAskedMs: NOW };
  assert.equal(onFreshResult(asked, true, NOW + ASK_GAP_MS - 1).ask, false);
  assert.equal(onFreshResult(asked, true, NOW + ASK_GAP_MS).ask, true);
});

test('stored state survives junk', () => {
  assert.deepEqual(parseState(null), EMPTY_STATE);
  assert.deepEqual(parseState('not json'), EMPTY_STATE);
  assert.deepEqual(parseState('{"goodResults":2,"lastAskedMs":5}'), { goodResults: 2, lastAskedMs: 5 });
});

test('the prompt is Apple\'s own, only on a fresh result, never from a button', () => {
  const lib = readFileSync(new URL('../src/lib/rating.ts', import.meta.url), 'utf8');
  const screen = readFileSync(new URL('../src/app/swing/[id].tsx', import.meta.url), 'utf8');
  assert.match(lib, /StoreReview\.requestReview\(\)/);
  assert.match(screen, /if \(!fresh \|\| !swing/);
  assert.doesNotMatch(screen, /onPress=\{[^}]*maybeAskForRating/);
});
