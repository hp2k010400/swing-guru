// The wiring boundaries root flagged: the request-identity rule (first-run bootstrap vs a changed
// owner), and the local teardown that must never sign out a replacement account.
import assert from 'node:assert/strict';
import { test } from 'node:test';

import { createAccountTeardown } from '../src/lib/account-teardown.ts';
import { createIdentityGate, resolveRequestIdentity } from '../src/lib/identity-gate.ts';

test('a cold-start request adopts the owner its bootstrap created', () => {
  const beforeBootstrap = { owner: null, generation: 0 };
  const afterBootstrap = { owner: 'anon-1', generation: 1 };
  assert.deepEqual(resolveRequestIdentity(beforeBootstrap, afterBootstrap),
    { ok: true, expected: afterBootstrap });
});

test('a request that started with a known owner refuses any change', () => {
  const started = { owner: 'user-a', generation: 4 };
  assert.equal(resolveRequestIdentity(started, started).ok, true);
  assert.equal(resolveRequestIdentity(started, { owner: 'user-b', generation: 5 }).ok, false,
    'a switch refuses the request');
  assert.equal(resolveRequestIdentity(started, { owner: 'user-a', generation: 6 }).ok, false,
    'a sign-out refuses the request even for the same id');
  assert.equal(resolveRequestIdentity(started, { owner: null, generation: 7 }).ok, false);
});

test('the identity gate bumps the generation on a switch and on sign-out', () => {
  const gate = createIdentityGate();
  gate.setOwner('user-a');
  const first = gate.snapshot();
  gate.setOwner('user-a');
  assert.deepEqual(gate.snapshot(), first, 'the same account is not a change');
  gate.setOwner('user-b');
  assert.equal(gate.unchanged(first), false);
  gate.invalidate();
  assert.equal(gate.unchanged(gate.snapshot()), true);
});

test('a teardown clears caches and signs out when the expected account is still signed in', async () => {
  const gate = createIdentityGate();
  gate.setOwner('user-a');
  const events: string[] = [];
  const teardown = createAccountTeardown({
    snapshot: () => gate.snapshot(),
    serialise: (operation) => operation(),
    forgetBillingIdentity: async () => { events.push('forget'); },
    signOutLocally: async () => { events.push('signOut'); gate.invalidate(); },
  });
  assert.equal(await teardown.run('user-a'), true);
  assert.deepEqual(events, ['forget', 'signOut']);
});

test('a late revocation teardown for A never signs out the B that replaced it', async () => {
  const gate = createIdentityGate();
  gate.setOwner('user-a');
  const events: string[] = [];
  let releaseForget!: () => void;
  const forget = new Promise<void>((done) => { releaseForget = done; });
  const teardown = createAccountTeardown({
    snapshot: () => gate.snapshot(),
    serialise: (operation) => operation(),
    forgetBillingIdentity: async () => { events.push('forget'); await forget; },
    signOutLocally: async () => { events.push('signOut'); },
  });
  const running = teardown.run('user-a');
  // The golfer signs in as B while the teardown awaits the billing clean-up.
  gate.setOwner('user-b');
  releaseForget();
  assert.equal(await running, false, 'the teardown reports that it did not sign anyone out');
  assert.deepEqual(events, ['forget'], 'B is still signed in');
});

test('a teardown refuses immediately when the expected account is already gone', async () => {
  const gate = createIdentityGate();
  gate.setOwner('user-b');
  const events: string[] = [];
  const teardown = createAccountTeardown({
    snapshot: () => gate.snapshot(),
    serialise: (operation) => operation(),
    forgetBillingIdentity: async () => { events.push('forget'); },
    signOutLocally: async () => { events.push('signOut'); },
  });
  assert.equal(await teardown.run('user-a'), false);
  assert.deepEqual(events, []);
});

test('the teardown runs inside the serialised transition queue', async () => {
  const gate = createIdentityGate();
  gate.setOwner('user-a');
  const events: string[] = [];
  const teardown = createAccountTeardown({
    snapshot: () => gate.snapshot(),
    serialise: async (operation) => { events.push('queued'); return operation(); },
    forgetBillingIdentity: async () => { events.push('forget'); },
    signOutLocally: async () => { events.push('signOut'); },
  });
  await teardown.run('user-a');
  assert.deepEqual(events, ['queued', 'forget', 'signOut']);
});
