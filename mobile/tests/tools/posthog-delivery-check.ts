// Diagnostic only (not part of the app or the test suite): drive the app's real analytics core
// (src/lib/analytics-core.ts) against PostHog EU with the production project key and print the HTTP
// result, so delivery can be checked without a phone.
//   node --experimental-strip-types tests/tools/posthog-delivery-check.ts <keyfile>
import { readFileSync } from 'node:fs';

import { createAnalytics } from '../../src/lib/analytics-core.ts';

const key = readFileSync(process.argv[2], 'utf8').trim();
const data = new Map<string, string>([['sg.analytics.consent', 'granted']]);
const runId = `sg-delivery-test-${Date.now()}`;

const client = createAnalytics({
  storage: () => ({ getItem: (k) => data.get(k) ?? null, setItem: (k, v) => { data.set(k, v); }, removeItem: (k) => { data.delete(k); } }),
  send: async (body) => {
    const res = await fetch('https://eu.i.posthog.com/batch/', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body });
    const text = await res.text();
    console.log('HTTP', res.status, '| response body:', text.slice(0, 200));
    const parsed = JSON.parse(body);
    console.log('sent events:', parsed.batch.map((e: { event: string; properties: Record<string, unknown> }) => `${e.event}(source=${e.properties.source})`).join(', '));
    console.log('distinct_id:', parsed.batch[0]?.properties?.distinct_id);
    return { ok: res.ok, status: res.status };
  },
  now: () => Date.now(),
  newId: () => runId,
  apiKey: key,
  base: () => ({ app_version: 'delivery-test', build: '0', $app_version: 'delivery-test', $app_build: '0', $os: 'iOS', $lib: 'swingguru-app' }),
  schedule: () => 0,
  cancel: () => undefined,
});

client.track('app_opened', { source: 'delivery_test', first_open: false });
await client.flush();
console.log('left in queue after flush:', client.pending());
