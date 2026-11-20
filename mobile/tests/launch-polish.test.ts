// Launch polish: account-safe detail cache, processing feedback decisions, haptic gating, replay
// state and score reveal. Pure logic only (no React Native), so it runs under node:test.
import assert from 'node:assert/strict';
import { test } from 'node:test';

import { createDetailCache } from '../src/lib/detail-cache.ts';
import {
  createHapticGate, createProgressTracker, replayReducer, revealFrom, type Snapshot,
} from '../src/lib/processing-state.ts';

const detail = (tag: string) => ({ tag, replay: { expires_in_s: 3600 } });

// ---------------------------------------------------------------- account-scoped cache

test('cache entries belong to the signed-in account and vanish on account change', () => {
  const cache = createDetailCache<ReturnType<typeof detail>>(() => 0);
  cache.setOwner('user-a');
  assert.equal(cache.put(cache.ticket(), 's1', detail('a')), true);
  assert.equal(cache.get('s1')?.tag, 'a');
  cache.setOwner('user-b');
  assert.equal(cache.get('s1'), null, 'user B never sees signed URLs fetched for user A');
  cache.setOwner('user-b');
  assert.equal(cache.size(), 0);
});

test('an in-flight response from the old account is refused after a switch or sign-out', () => {
  const cache = createDetailCache<ReturnType<typeof detail>>(() => 0);
  cache.setOwner('user-a');
  const before = cache.ticket();
  cache.setOwner('user-b'); // switched while the request was in flight
  assert.equal(cache.put(before, 's1', detail('a')), false);
  assert.equal(cache.get('s1'), null);
  const b = cache.ticket();
  cache.clear(); // sign-out
  assert.equal(cache.put(b, 's2', detail('b')), false, 'nothing is cached after sign-out');
  cache.setOwner('user-b'); // the same user signs back in: still nothing from before
  assert.equal(cache.get('s2'), null);
});

test('signed URLs are only served while valid, with a safety margin', () => {
  let t = 0;
  const cache = createDetailCache<ReturnType<typeof detail>>(() => t, 600);
  cache.setOwner('user-a');
  cache.put(cache.ticket(), 's1', detail('a'));
  t = (3600 - 601) * 1000;
  assert.ok(cache.get('s1'));
  t = (3600 - 599) * 1000;
  assert.equal(cache.get('s1'), null);
});

test('a signed-out cache stores nothing', () => {
  const cache = createDetailCache<ReturnType<typeof detail>>(() => 0);
  assert.equal(cache.put(cache.ticket(), 's1', detail('x')), false);
});

// ---------------------------------------------------------------- processing state machine

const STAGES = ['prepare', 'moments', 'club', 'positions', 'build'];
const snap = (status: Snapshot['status'], done: string[], uploadDone = true, jobId = 'job-1'): Snapshot => ({
  jobId, status, uploadDone,
  stages: STAGES.map((id) => ({ id, state: done.includes(id) ? 'done' : 'pending' })),
});

test('hydration never announces stages that were already done', () => {
  const tracker = createProgressTracker();
  assert.equal(tracker.observe(snap('processing', ['prepare', 'moments'])), null);
  assert.equal(tracker.observe(snap('processing', ['prepare', 'moments'])), null, 'repeated polls stay silent');
});

test('one cue per poll, however many stages finished between polls', () => {
  const tracker = createProgressTracker();
  tracker.observe(snap('processing', []));
  assert.equal(tracker.observe(snap('processing', ['prepare', 'moments', 'club'])), 'stage');
  assert.equal(tracker.observe(snap('processing', ['prepare', 'moments', 'club'])), null);
  assert.equal(tracker.observe(snap('processing', ['prepare', 'moments', 'club', 'positions'])), 'stage');
});

test('ready only when the job is complete, once, even if every stage was already done', () => {
  const tracker = createProgressTracker();
  tracker.observe(snap('processing', ['prepare', 'moments', 'club', 'positions']));
  assert.equal(tracker.observe(snap('processing', STAGES)), 'stage');
  assert.equal(tracker.observe(snap('complete', STAGES)), 'ready');
  assert.equal(tracker.observe(snap('complete', STAGES)), null);
});

test('a job that was already complete when opened is not announced', () => {
  const tracker = createProgressTracker();
  assert.equal(tracker.observe(snap('complete', STAGES)), null);
});

test('returning from the background absorbs progress silently but still announces ready', () => {
  const tracker = createProgressTracker();
  tracker.observe(snap('processing', ['prepare']));
  assert.equal(tracker.observe(snap('processing', ['prepare', 'moments']), false), null, 'no cue while backgrounded');
  tracker.quietNext();
  assert.equal(tracker.observe(snap('processing', ['prepare', 'moments', 'club', 'positions'])), null, 'no catch-up burst');
  assert.equal(tracker.observe(snap('processing', STAGES)), 'stage');
  tracker.quietNext();
  assert.equal(tracker.observe(snap('complete', STAGES)), 'ready');
});

test('a different job starts fresh (hydrates again)', () => {
  const tracker = createProgressTracker();
  tracker.observe(snap('processing', []));
  assert.equal(tracker.observe(snap('processing', ['prepare', 'moments'], true, 'job-2')), null);
  assert.equal(tracker.observe(snap('processing', ['prepare', 'moments', 'club'], true, 'job-2')), 'stage');
});

test('upload completing counts as a real stage', () => {
  const tracker = createProgressTracker();
  tracker.observe(snap('awaiting_upload', [], false));
  assert.equal(tracker.observe(snap('queued', [], true)), 'stage');
});

// ---------------------------------------------------------------- haptics

test('haptics: stage cues rate limited, success once per job, reveal skipped right after success', () => {
  let t = 0;
  const gate = createHapticGate(() => t, 1200, 4000);
  assert.equal(gate.allow('stage'), true);
  t = 500;
  assert.equal(gate.allow('stage'), false, 'no vibration spam');
  t = 1800;
  assert.equal(gate.allow('stage'), true);
  assert.equal(gate.allow('success', 'job-1'), true);
  assert.equal(gate.allow('success', 'job-1'), false, 'ready is never re-announced (poll or remount)');
  t = 3000;
  assert.equal(gate.allow('reveal', 'job-1'), false, 'no duplicate ready + score buzz');
  t = 60_000;
  assert.equal(gate.allow('reveal', 'job-1'), false, 'and not later on a remount either');
  assert.equal(gate.allow('reveal', 'job-2'), true, 'a reveal on its own gets one cue');
  assert.equal(gate.allow('selection'), true);
});

// ---------------------------------------------------------------- replay and score reveal

test('replay: watch again resets ready/ended and ignores messages from the old run', () => {
  let state = { run: 0, ready: false, ended: false };
  state = replayReducer(state, { type: 'message', run: 0, kind: 'ready' });
  state = replayReducer(state, { type: 'message', run: 0, kind: 'ended' });
  assert.deepEqual(state, { run: 0, ready: true, ended: true });
  state = replayReducer(state, { type: 'watchAgain' });
  assert.deepEqual(state, { run: 1, ready: false, ended: false });
  const stale = replayReducer(state, { type: 'message', run: 0, kind: 'ended' });
  assert.equal(stale, state, 'a late message from the previous viewer is ignored');
  const again = replayReducer(state, { type: 'message', run: 1, kind: 'ended' });
  assert.equal(replayReducer(again, { type: 'message', run: 1, kind: 'ended' }), again, 'duplicate end is a no-op');
});

test('score reveal shows the stored final value at once under reduced motion or for history', () => {
  assert.equal(revealFrom(7.4, { fresh: true, reducedMotion: false }), 0);
  assert.equal(revealFrom(7.4, { fresh: true, reducedMotion: true }), 7.4);
  assert.equal(revealFrom(7.4, { fresh: false, reducedMotion: false }), 7.4);
});
