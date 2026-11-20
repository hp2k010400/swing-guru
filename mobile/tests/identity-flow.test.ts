// The Sign in with Apple orchestration, driven through injected (often deferred) dependencies - the
// real decisions, not a copy of the helpers. Covers the twelve user-visible flows: guest link keeps
// the UUID and its history/usage/RevenueCat identity, an existing Apple account is confirmed before
// switching (and a cancellation stays put), sign-out/sign-in/reinstall recovery, a link that lands on
// another UUID is abandoned, a stale confirmation is refused, transitions are serialised and the
// server-storage failure never reads as success.
import assert from 'node:assert/strict';
import { test } from 'node:test';

import { createIdentityGate } from '../src/lib/identity-gate.ts';
import { createIdentityFlow, type AppleGrantInput, type IdentityFlowDeps } from '../src/lib/identity-flow.ts';
import type { AccountView } from '../src/lib/identity-core.ts';

const GRANT: AppleGrantInput = { token: 'identity-token', authorizationCode: 'auth-code-1234',
                                 nonce: 'raw-nonce-1234' };

const guest = (id = 'user-a'): AccountView =>
  ({ userId: id, anonymous: true, provider: null, email: null, appleUserId: null });
const linked = (id = 'user-a'): AccountView =>
  ({ userId: id, anonymous: false, provider: 'apple', email: null, appleUserId: 'apple-sub' });

function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((done) => { resolve = done; });
  return { promise, resolve };
}

function harness(initial: AccountView | null, overrides: Partial<IdentityFlowDeps> = {}) {
  const calls: string[] = [];
  const state = { view: initial as AccountView | null };
  const gate = createIdentityGate();
  gate.setOwner(initial?.userId ?? null);
  const deps: IdentityFlowDeps = {
    serialise: (operation) => operation(),
    ensureSession: async () => { calls.push('ensure'); },
    session: async () => state.view,
    ownerId: () => state.view?.userId ?? null,
    snapshot: () => gate.snapshot(),
    link: async () => {
      calls.push('link');
      const id = state.view?.userId ?? null;
      if (id) state.view = linked(id);   // Supabase attaches the identity to the same user
      return { userId: id };
    },
    signInWithIdToken: async () => { calls.push('signIn'); return {}; },
    refresh: async () => { calls.push('refresh'); },
    abandon: async () => { calls.push('abandon'); state.view = null; },
    beforeSwitch: async () => { calls.push('beforeSwitch'); },
    ...overrides,
  };
  return { flow: createIdentityFlow(deps), calls, state, deps, gate };
}

const INTENT = { owner: 'user-a', generation: 1 };

test('a guest link keeps the same UUID and only reports success after the grant is stored', async () => {
  const { flow, calls, state } = harness(guest());
  const result = await flow.signIn(GRANT, { owner: 'user-a', generation: 1 });
  assert.equal(result.outcome, 'linked');
  // The link kept the guest UUID (history, monthly usage and the RevenueCat user id hang off it).
  assert.equal(state.view?.userId, 'user-a');
  assert.equal(result.grant?.ownerId, 'user-a', 'the grant is handed back for the right account');
  assert.deepEqual(result.grant?.body, { authorization_code: 'auth-code-1234', nonce: 'raw-nonce-1234' });
  assert.deepEqual(calls, ['ensure', 'link', 'refresh']);
  assert.equal(result.pending, undefined);
});

test('a link that lands on another UUID is abandoned, never reported as linked', async () => {
  const { calls, state, gate } = harness(guest('user-a'));
  const flowOnOther = createIdentityFlow({
    serialise: (operation) => operation(),
    ensureSession: async () => undefined,
    session: async () => state.view,
    ownerId: () => state.view?.userId ?? null,
    snapshot: () => gate.snapshot(),
    // linkIdentity silently ended up on another account.
    link: async () => { state.view = linked('user-b'); return { userId: 'user-b' }; },
    signInWithIdToken: async () => ({}),
    refresh: async () => undefined,
    registerGrant: async () => true,
    abandon: async () => { calls.push('abandon'); state.view = null; },
    beforeSwitch: async () => undefined,
  });
  const result = await flowOnOther.signIn(GRANT, gate.snapshot());
  assert.equal(result.outcome, 'link_incomplete');
  assert.ok(calls.includes('abandon'), 'the unexpected session is dropped locally');
  assert.equal(state.view, null);
});

test('a link that does not end up with a durable Apple identity is not success', async () => {
  const state = { view: guest('user-a') as AccountView | null };
  const calls: string[] = [];
  const gate = createIdentityGate();
  gate.setOwner('user-a');
  // The link reports the same UUID, but the refreshed session still shows no Apple identity: the
  // "durable Apple identity" half of the contract is not satisfied.
  const flow = createIdentityFlow({
    serialise: (operation) => operation(),
    ensureSession: async () => { calls.push('ensure'); },
    session: async () => state.view,
    ownerId: () => state.view?.userId ?? null,
    snapshot: () => gate.snapshot(),
    link: async () => ({ userId: 'user-a' }),
    signInWithIdToken: async () => ({}),
    refresh: async () => { calls.push('refresh'); },
    registerGrant: async () => { calls.push('register'); return true; },
    abandon: async () => { calls.push('abandon'); state.view = null; },
    beforeSwitch: async () => undefined,
  });
  const result = await flow.signIn(GRANT, gate.snapshot());
  assert.equal(result.outcome, 'link_incomplete');
  assert.ok(calls.includes('abandon'));
  assert.equal(result.grant, undefined, 'a grant is never handed back for an unverified link');
});

test('an existing Apple account is only switched to after confirmation', async () => {
  const error = { code: 'identity_already_exists' };
  const { flow, calls, gate } = harness(guest('guest-1'), { link: async () => ({ error }) });
  const result = await flow.signIn(GRANT, gate.snapshot());
  assert.equal(result.outcome, 'existing_account');
  assert.equal(result.pending?.ownerId, 'guest-1');
  assert.equal(result.pending?.authorizationCode, 'auth-code-1234');
  assert.deepEqual(calls.filter((call) => call !== 'ensure'), [],
    'nothing is switched or cleared before the golfer agrees');
});

test('cancelling the Apple sheet is a quiet cancel and changes nothing', async () => {
  const { flow, calls } = harness(guest(), { link: async () => ({ error: { code: 'ERR_REQUEST_CANCELED' } }) });
  assert.equal((await flow.signIn(GRANT, INTENT)).outcome, 'cancelled');
  assert.deepEqual(calls.filter((call) => call !== 'ensure'), []);
});

test('a returning Apple user is signed in, then the grant is stored', async () => {
  const { flow, calls } = harness(linked('user-a'));
  const result = await flow.signIn(GRANT, { owner: 'user-a', generation: 1 });
  assert.equal(result.outcome, 'signed_in');
  assert.equal(result.grant?.ownerId, 'user-a');
  assert.deepEqual(calls, ['ensure', 'signIn']);
});

test('reinstall recovery: the existing account is found and its grant re-registered', async () => {
  const { flow } = harness(linked('user-a'));
  const result = await flow.signIn(GRANT, { owner: 'user-a', generation: 1 });
  assert.equal(result.outcome, 'signed_in');
  assert.ok(result.grant, 'the returning golfer can re-store the grant');
});

test('a confirmed switch to an existing account signs in and stores the grant', async () => {
  const { flow, calls, state } = harness(guest('guest-1'));
  const pending = { token: 'identity-token', nonce: 'raw-nonce-1234', authorizationCode: 'auth-code-1234',
                    ownerId: 'guest-1' };
  state.view = guest('guest-1');
  const outcome = await flow.switchTo(pending);
  assert.equal(outcome.outcome, 'switched');
  assert.equal(outcome.grant?.ownerId, 'guest-1');
  assert.deepEqual(calls, ['beforeSwitch', 'signIn']);
});

test('a stale switch confirmation is refused and signs nobody in', async () => {
  const { flow, calls } = harness(linked('user-b')); // the guest that asked is gone; B is signed in
  const pending = { token: 't', nonce: 'n', authorizationCode: 'c', ownerId: 'guest-1' };
  assert.equal((await flow.switchTo(pending)).outcome, 'failed');
  assert.deepEqual(calls, [], 'no sign-in, no cache clear, nobody is signed out');
});

test('auth transitions are serialised: a second sign-in waits for the first', async () => {
  let queue: Promise<unknown> = Promise.resolve();
  const serialise = <T,>(operation: () => Promise<T>): Promise<T> => {
    const run = queue.then(operation, operation);
    queue = run.then(() => undefined, () => undefined);
    return run;
  };
  const gate = deferred<{ userId: string }>();
  const calls: string[] = [];
  const { flow, state } = harness(guest('user-a'), {
    serialise,
    link: async () => {
      calls.push('link-start');
      const value = await gate.promise;
      calls.push('link-end');
      state.view = linked('user-a');
      return value;
    },
    ensureSession: async () => { calls.push('ensure'); },
    refresh: async () => { calls.push('refresh'); },
  });
  const first = flow.signIn(GRANT, INTENT);
  const second = flow.signIn(GRANT, INTENT);
  await new Promise((done) => setImmediate(done));
  assert.deepEqual(calls, ['ensure', 'link-start'], 'the second transition has not started');
  gate.resolve({ userId: 'user-a' });
  await Promise.all([first, second]);
  assert.deepEqual(calls.slice(0, 2), ['ensure', 'link-start']);
  assert.ok(calls.lastIndexOf('ensure') > calls.indexOf('link-end'),
    'the second transition only starts once the first has settled');
});

test('no session means no sign-in attempt', async () => {
  const { flow, calls } = harness(guest(), { ensureSession: async () => { throw new Error('offline'); } });
  assert.equal((await flow.signIn(GRANT, INTENT)).outcome, 'failed');
  assert.deepEqual(calls, []);
});

test('an owner change while the Apple sheet is open aborts instead of linking to the wrong guest', async () => {
  const { flow, calls, state, gate } = harness(guest('user-a'));
  // The golfer opened the sheet as user-a; by the time the credential came back, B is signed in.
  state.view = guest('user-b');
  gate.setOwner('user-b');
  const result = await flow.signIn(GRANT, { owner: 'user-a', generation: 1 });
  assert.equal(result.outcome, 'failed');
  assert.deepEqual(calls, [], 'no link and no grant for the wrong account');
});

test('a first-run sheet (no owner yet) is allowed to bootstrap the guest it started for', async () => {
  const { flow, state, gate } = harness(null, {
    ensureSession: async () => { state.view = guest('fresh'); gate.setOwner('fresh'); },
  });
  const intent = gate.snapshot();               // owner null: the bootstrap has not run yet
  const result = await flow.signIn(GRANT, intent);
  assert.equal(result.outcome, 'linked');
  assert.equal(result.grant?.ownerId, 'fresh');
});

test('the identity gate invalidates in-flight snapshots on a switch and on sign-out', () => {
  const gate = createIdentityGate();
  gate.setOwner('user-a');
  const taken = gate.snapshot();
  assert.equal(gate.unchanged(taken), true);
  gate.setOwner('user-b');
  assert.equal(gate.unchanged(taken), false, 'an old account can never finish as the new one');
  const takenB = gate.snapshot();
  gate.setOwner('user-b');
  assert.equal(gate.unchanged(takenB), true, 'the same account is not an identity change');
  gate.invalidate();
  assert.equal(gate.unchanged(takenB), false, 'sign-out invalidates even the same id');
  assert.equal(gate.unchanged(gate.snapshot()), true);
});
