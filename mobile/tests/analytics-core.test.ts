import assert from 'node:assert/strict';
import test from 'node:test';

import { createAnalytics, KEYS, OPEN_GAP_MS, type CoreDeps, type KV } from '../src/lib/analytics-core';

function memory(): KV & { data: Map<string, string> } {
  const data = new Map<string, string>();
  return {
    data,
    getItem: (k) => data.get(k) ?? null,
    setItem: (k, v) => { data.set(k, v); },
    removeItem: (k) => { data.delete(k); },
  };
}

function harness(opts: { status?: number; throws?: boolean; key?: string } = {}) {
  const kv = memory();
  const sent: { batch: { event: string; properties: Record<string, unknown> }[] }[] = [];
  let clock = 1_000_000;
  let response = { status: opts.status ?? 200, throws: opts.throws ?? false };
  const deps: CoreDeps = {
    storage: () => kv,
    send: async (body) => {
      if (response.throws) throw new Error('offline');
      sent.push(JSON.parse(body));
      return { ok: response.status < 300, status: response.status };
    },
    now: () => clock,
    newId: () => 'anon-1',
    apiKey: opts.key ?? 'phc_test',
    base: () => ({ app_version: '1.0.2', build: '26' }),
    schedule: () => 1,
    cancel: () => undefined,
  };
  const a = createAnalytics(deps);
  const events = () => sent.flatMap((s) => s.batch.map((e) => e.event));
  return {
    a, kv, sent, events,
    tick: (ms: number) => { clock += ms; },
    respond: (next: { status?: number; throws?: boolean }) => { response = { status: next.status ?? 200, throws: next.throws ?? false }; },
  };
}

test('nothing is queued or sent without consent', async () => {
  const h = harness();
  h.a.track('app_opened');
  h.a.appOpened('cold_start');
  await h.a.flush();
  assert.equal(h.sent.length, 0);
  h.a.setConsent('denied');
  h.a.track('paywall_viewed');
  await h.a.flush();
  assert.equal(h.sent.length, 0);
});

test('no project key: analytics stays off even with consent', async () => {
  const h = harness({ key: '' });
  h.a.setConsent('granted');
  h.a.track('app_opened');
  await h.a.flush();
  assert.equal(h.sent.length, 0);
});

test('granting consent counts the current visit once, with version properties and no person profile', async () => {
  const h = harness();
  h.a.appOpened('cold_start');      // dropped: consent unset
  h.a.setConsent('granted');        // counts this visit
  await h.a.flush();
  assert.deepEqual(h.events(), ['app_opened']);
  const props = h.sent[0].batch[0].properties;
  assert.equal(props.source, 'consent');
  assert.equal(props.first_open, true);
  assert.equal(props.app_version, '1.0.2');
  assert.equal(props.distinct_id, 'anon-1');
  assert.equal(props.$process_person_profile, false);
});

test('foreground returns within 30 s are the same visit; later ones are new opens', async () => {
  const h = harness();
  h.a.appOpened('cold_start');      // first launch, consent unset: not sent
  h.a.setConsent('granted');        // the same visit is counted now
  h.tick(5_000);
  h.a.appOpened('foreground');      // same visit
  h.tick(OPEN_GAP_MS + 1);
  h.a.appOpened('foreground');      // new open
  h.tick(10_000);
  h.a.setConsent('granted');        // toggling Allow again doesn't add an open
  await h.a.flush();
  const opens = h.sent.flatMap((s) => s.batch).filter((e) => e.event === 'app_opened');
  assert.deepEqual(opens.map((e) => e.properties.source), ['consent', 'foreground']);
  assert.deepEqual(opens.map((e) => e.properties.first_open), [true, false]);
});

test('first_open is only true for the first launch of an install', async () => {
  const h = harness();
  h.kv.setItem(KEYS.consent, 'granted');
  h.a.appOpened('cold_start');      // first launch of this install
  // A later launch (new process, same device storage):
  let firstOpen: unknown = 'unset';
  const spy = createAnalytics({
    storage: () => h.kv,
    send: async (body) => { firstOpen = JSON.parse(body).batch[0].properties.first_open; return { ok: true, status: 200 }; },
    now: () => 9e12, newId: () => 'x', apiKey: 'phc_test', base: () => ({}), schedule: () => 1, cancel: () => undefined,
  });
  spy.appOpened('cold_start');
  await spy.flush();
  assert.equal(firstOpen, false);
});

test('trackOnce: one completion per swing, even across a restart', async () => {
  const h = harness();
  h.kv.setItem(KEYS.consent, 'granted');
  h.a.trackOnce('swing_analysis_completed', 'job-1', { score_band: '7-8' });
  h.a.trackOnce('swing_analysis_completed', 'job-1', { score_band: '7-8' });
  h.a.trackOnce('swing_analysis_completed', 'job-2');
  await h.a.flush();
  assert.deepEqual(h.events(), ['swing_analysis_completed', 'swing_analysis_completed']);
  const restarted = createAnalytics({
    storage: () => h.kv, send: async () => ({ ok: true, status: 200 }), now: () => 1, newId: () => 'x',
    apiKey: 'phc_test', base: () => ({}), schedule: () => 1, cancel: () => undefined,
  });
  assert.equal(restarted.trackOnce('swing_analysis_completed', 'job-1'), false);
});

test('trackOnce does not burn the key while consent is missing', () => {
  const h = harness();
  assert.equal(h.a.trackOnce('swing_analysis_completed', 'job-1'), false);
  h.a.setConsent('granted');
  assert.equal(h.a.trackOnce('swing_analysis_completed', 'job-1'), true);
});

test('offline and 5xx sends are retried, 4xx are dropped, nothing is sent twice', async () => {
  const h = harness();
  h.kv.setItem(KEYS.consent, 'granted');
  h.a.track('paywall_viewed');
  h.respond({ throws: true });
  await h.a.flush();
  assert.equal(h.a.pending(), 1);
  h.respond({ status: 503 });
  await h.a.flush();
  assert.equal(h.a.pending(), 1);
  h.respond({ status: 200 });
  await h.a.flush();
  assert.equal(h.a.pending(), 0);
  h.a.track('league_joined');
  h.respond({ status: 400 });
  await h.a.flush();
  assert.equal(h.a.pending(), 0);
  assert.equal(h.events().filter((e) => e === 'paywall_viewed').length, 2); // 503 attempt + success
});

test('withdrawing consent drops queued and retry events', async () => {
  const h = harness();
  h.kv.setItem(KEYS.consent, 'granted');
  h.a.track('paywall_viewed');
  h.respond({ throws: true });
  await h.a.flush();
  h.a.setConsent('denied');
  h.respond({ status: 200 });
  await h.a.flush();
  assert.equal(h.sent.length, 0);
  assert.equal(h.a.pending(), 0);
});
