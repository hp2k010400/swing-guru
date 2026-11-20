// The durable-account decisions the app makes around Apple registration and account deletion.
// Pure logic only (no React Native, no SDK), mirroring account-core.ts.
import assert from 'node:assert/strict';
import { test } from 'node:test';

import {
  appleCredentialBody, deletionCopy, deletionOutcomeFrom, type DeletionResult,
} from '../src/lib/account-core.ts';

const DELETED = (revocation: DeletionResult['apple_revocation']): DeletionResult =>
  ({ status: 'deleted', apple_revocation: revocation });

test('the Apple grant body carries only the code and the raw nonce', () => {
  assert.deepEqual(appleCredentialBody({ authorizationCode: 'code-123', nonce: 'raw-nonce' }),
    { authorization_code: 'code-123', nonce: 'raw-nonce' });
  // Apple can omit the code; then the app must not pretend the grant was registered.
  assert.equal(appleCredentialBody({ authorizationCode: null, nonce: 'raw-nonce' }), null);
  assert.equal(appleCredentialBody({ authorizationCode: '   ', nonce: 'raw-nonce' }), null);
  assert.equal(appleCredentialBody({ authorizationCode: 'code-123', nonce: '' }), null);
});

test('a confirmed revocation and a manual fallback are different outcomes', () => {
  assert.equal(deletionOutcomeFrom(DELETED('revoked'), null), 'deleted');
  assert.equal(deletionOutcomeFrom(DELETED('not_required'), null), 'deleted');
  // Apple's TN3194 fallback: the data is gone but the server did not revoke anything.
  assert.equal(deletionOutcomeFrom(DELETED('manual_required'), null), 'deleted_manual_apple');
  const manual = deletionCopy('deleted_manual_apple')!;
  assert.match(manual.body, /Settings/);
  assert.match(manual.body, /Stop Using Apple ID/);
});

test('a legacy Apple account is offered the fresh authorization before any fallback', () => {
  assert.equal(deletionOutcomeFrom(null, { status: 409, code: 'apple_reauthorization_required' }), 'reauthorize');
  assert.match(deletionCopy('reauthorize')!.body, /sign in with Apple again/i);
});

test('partial deletion and an unreachable Apple stay retryable, never "deleted"', () => {
  for (const code of ['delete_incomplete', 'apple_unavailable', 'temporarily_unavailable']) {
    assert.equal(deletionOutcomeFrom(null, { status: 503, code }), 'retry', code);
  }
  const retry = deletionCopy('retry')!;
  assert.match(retry.body, /safe to repeat/i);
});

test('an account deleted elsewhere finishes the local teardown too', () => {
  assert.equal(deletionOutcomeFrom(null, { status: 403, code: 'account_deleted' }), 'already_deleted');
  assert.equal(deletionOutcomeFrom(null, { status: 500, code: 'server_error' }), 'failed');
});
