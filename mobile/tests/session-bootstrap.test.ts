// Session initialization: two cold-start callers must share one anonymous sign-in.
import assert from 'node:assert/strict';
import { test } from 'node:test';

import { createSessionBootstrap, singleFlight } from '../src/lib/session-bootstrap.ts';

function gate(): { promise: Promise<void>; release: () => void } {
  let release: () => void = () => undefined;
  const promise = new Promise<void>((resolve) => {
    release = resolve;
  });
  return { promise, release };
}

test('singleFlight shares one in-flight operation and runs again afterwards', async () => {
  let runs = 0;
  const hold = gate();
  const run = singleFlight(async () => {
    runs += 1;
    await hold.promise;
    return runs;
  });
  const first = run();
  const second = run();
  hold.release();
  assert.deepEqual(await Promise.all([first, second]), [1, 1]);
  assert.equal(runs, 1);
  assert.equal(await run(), 2);
  assert.equal(runs, 2);
});

test('singleFlight does not cache a failure', async () => {
  let attempts = 0;
  const run = singleFlight(async () => {
    attempts += 1;
    if (attempts === 1) throw new Error('sign-in failed');
    return attempts;
  });
  await assert.rejects(() => run(), /sign-in failed/);
  assert.equal(await run(), 2);
});

test('two cold-start callers create exactly one session', async () => {
  const hold = gate();
  let created = 0;
  let session: { id: string } | null = null;
  const bootstrap = createSessionBootstrap({
    readSession: async () => session,
    createSession: async () => {
      created += 1;
      await hold.promise;
      session = { id: 'user-anon-1' };
      return session;
    },
  });
  const first = bootstrap();
  const second = bootstrap();
  hold.release();
  const [a, b] = await Promise.all([first, second]);
  assert.equal(created, 1);
  assert.equal(a?.id, 'user-anon-1');
  assert.equal(b?.id, 'user-anon-1');
  assert.equal(a, b);
});

test('a slow read cannot start a second anonymous session', async () => {
  // Regression: with only the creation single-flighted, the second caller's own (slow) read could
  // resolve to null after the first creation had settled and cleared, starting a second
  // anonymous account. Single-flighting the whole resolution means the second caller joins the
  // first read-or-create and its read never runs.
  let reads = 0;
  let created = 0;
  let session: { id: string } | null = null;
  const holdCreate = gate();
  const bootstrap = createSessionBootstrap({
    readSession: async () => {
      reads += 1;
      return session;
    },
    createSession: async () => {
      created += 1;
      await holdCreate.promise;
      session = { id: `anon-${created}` };
      return session;
    },
  });
  const first = bootstrap();
  const second = bootstrap();
  holdCreate.release();
  const [a, b] = await Promise.all([first, second]);
  assert.equal(reads, 1, 'the second caller must not run its own read');
  assert.equal(created, 1, 'only one anonymous account may be created');
  assert.equal(a?.id, 'anon-1');
  assert.equal(a, b);
  // A later call re-reads the now-existing session and creates nothing.
  assert.equal((await bootstrap())?.id, 'anon-1');
  assert.equal(reads, 2);
  assert.equal(created, 1);
});

test('an existing session is reused without creating another', async () => {
  let created = 0;
  const bootstrap = createSessionBootstrap({
    readSession: async () => ({ id: 'existing' }),
    createSession: async () => {
      created += 1;
      return { id: 'new' };
    },
  });
  assert.equal((await bootstrap())?.id, 'existing');
  assert.equal(created, 0);
});

test('after sign-out a fresh session is created on the next call', async () => {
  let created = 0;
  let session: { id: string } | null = { id: 'first' };
  const bootstrap = createSessionBootstrap({
    readSession: async () => session,
    createSession: async () => {
      created += 1;
      session = { id: `anon-${created}` };
      return session;
    },
  });
  assert.equal((await bootstrap())?.id, 'first');
  session = null; // sign-out
  assert.equal((await bootstrap())?.id, 'anon-1');
  assert.equal(created, 1);
});
