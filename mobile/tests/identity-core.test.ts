// Sign in with Apple decisions: error classification, copy and account view (no tokens exposed).
import assert from 'node:assert/strict';
import { test } from 'node:test';

import { accountView, classifyAuthError, outcomeCopy } from '../src/lib/identity-core.ts';

test('an Apple ID that already has an account is detected, so the app asks before switching', () => {
  assert.equal(classifyAuthError({ code: 'identity_already_exists', status: 422 }), 'existing_account');
  assert.equal(classifyAuthError({ message: 'Identity is already linked to another user' }), 'existing_account');
});

test('closing the Apple sheet is a quiet cancel, not an error', () => {
  assert.equal(classifyAuthError({ code: 'ERR_REQUEST_CANCELED' }), 'cancelled');
  assert.equal(outcomeCopy('cancelled'), null);
});

test('a project without Apple or manual linking enabled says so instead of failing vaguely', () => {
  assert.equal(classifyAuthError({ code: 'manual_linking_disabled' }), 'not_configured');
  assert.equal(classifyAuthError({ code: 'provider_disabled' }), 'not_configured');
  assert.equal(classifyAuthError({ message: 'Unsupported provider: provider is not enabled' }), 'not_configured');
  assert.equal(classifyAuthError({ code: 'unexpected_failure', status: 500 }), 'failed');
  assert.equal(classifyAuthError(null), 'failed');
});

test('linking keeps the promise about swings and free analyses; switching warns about guest swings', () => {
  assert.match(outcomeCopy('linked')!.body, /reinstall/);
  assert.match(outcomeCopy('existing_account')!.body, /guest profile/);
});

test('account view: guest, Apple-linked guest and signed-out states', () => {
  assert.deepEqual(accountView(null),
    { userId: null, anonymous: true, provider: null, email: null, appleUserId: null });
  assert.deepEqual(accountView({ id: 'u1', is_anonymous: true, identities: [] }),
    { userId: 'u1', anonymous: true, provider: null, email: null, appleUserId: null });
  // After linkIdentity the same user id carries an apple identity.
  assert.deepEqual(
    accountView({ id: 'u1', is_anonymous: false, email: 'x@privaterelay.appleid.com',
                  identities: [{ provider: 'apple', identity_data: { sub: '001234.abcdef.0900' } }] }),
    { userId: 'u1', anonymous: false, provider: 'apple', email: 'x@privaterelay.appleid.com',
      appleUserId: '001234.abcdef.0900' });
});

test('a signed-in account that could not store its Apple grant is not reported as success', () => {
  const copy = outcomeCopy('link_incomplete');
  assert.ok(copy && /Sign in with Apple/.test(copy.body), 'the golfer is told how to finish');
  assert.notEqual(classifyAuthError({ code: 'unexpected_failure' }), 'link_incomplete');
});
