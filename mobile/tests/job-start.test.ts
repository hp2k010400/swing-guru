// The /start failure decisions: the bytes are already uploaded, so only a genuinely missing
// upload should send the golfer back to their camera roll.
import assert from 'node:assert/strict';
import { test } from 'node:test';

import { ANALYSES_IN_PROGRESS_COPY, SIGN_IN_REQUIRED_COPY } from '../src/lib/billing-core.ts';
import {
  classifyStartFailure, errorPrompt, nextPhaseForRetry, shouldKeepDraft, UPLOAD_MISSING_AFTER_MS, UPLOAD_TTL_HOURS,
} from '../src/lib/job-start.ts';

test('a 402 keeps the uploaded job and asks for a plan', () => {
  const decision = classifyStartFailure({ status: 402, code: 'purchase_required' });
  assert.equal(decision.phase, 'purchase_required');
  assert.equal(decision.retrySameJob, true);
  assert.equal(decision.chooseAgain, false);
  assert.equal(decision.message, null);
  assert.equal(shouldKeepDraft('purchase_required'), true);
  assert.equal(shouldKeepDraft('billing_unavailable'), false);
});

test('a 503 or an offline failure keeps the job for a retry of the same job', () => {
  for (const error of [{ status: 503, code: 'billing_unavailable' }, { status: 0, code: 'offline' }, { status: 502 }]) {
    const decision = classifyStartFailure(error, 5_000);
    assert.equal(decision.phase, 'uploaded', JSON.stringify(error));
    assert.equal(decision.retrySameJob, true);
    assert.equal(decision.chooseAgain, false);
    assert.ok(decision.message);
  }
});

test('402 then purchase then retry never discards the job', () => {
  const before = classifyStartFailure({ status: 402, code: 'purchase_required' });
  const after = classifyStartFailure({ status: 503, code: 'billing_unavailable' }, 30_000);
  assert.equal(before.retrySameJob, true);
  assert.equal(after.retrySameJob, true);
  assert.notEqual(before.phase, 'upload_failed');
  assert.notEqual(after.phase, 'upload_failed');
});

test('a missing upload keeps waiting while it may still be arriving, then needs the video again', () => {
  const fresh = classifyStartFailure({ status: 409, code: 'upload_missing' }, 2_000);
  assert.equal(fresh.phase, 'uploaded');
  assert.equal(fresh.retrySameJob, true);
  // A slow upload (minutes) is not lost: this used to fail at 60 s and showed "couldn't find your upload".
  assert.equal(classifyStartFailure({ status: 409, code: 'upload_missing' }, 5 * 60_000).phase, 'uploaded');
  const stale = classifyStartFailure({ status: 409, code: 'upload_missing' }, UPLOAD_MISSING_AFTER_MS + 1);
  assert.equal(stale.phase, 'upload_failed');
  assert.equal(stale.chooseAgain, true);
  // 30 minutes is not the 24-hour TTL: say only what is known.
  assert.match(String(stale.message), /couldn't find your upload/i);
  assert.ok(!new RegExp(`${UPLOAD_TTL_HOURS} hours`).test(String(stale.message)));
});

test('an expired job explains the TTL and never promises indefinite storage', () => {
  const decision = classifyStartFailure({ status: 404, code: 'not_found' }, 0);
  assert.equal(decision.phase, 'upload_failed');
  assert.equal(decision.chooseAgain, true);
  assert.match(String(decision.message), new RegExp(`${UPLOAD_TTL_HOURS} hours`));
  assert.ok(!/indefinite|forever|unlimited/i.test(String(decision.message)));
});

test('an unknown failure asks for the video again', () => {
  const decision = classifyStartFailure({ status: 400, code: 'invalid_request' }, 0);
  assert.equal(decision.phase, 'upload_failed');
  assert.equal(decision.chooseAgain, true);
  assert.ok(decision.message);
});

test('a retryable failure unsticks a pending purchase_required job', () => {
  const retry = classifyStartFailure({ status: 503, code: 'billing_unavailable' }, 1_000);
  // After a purchase the plan exists, so a 503 must return the job to the polling loop rather
  // than leaving it behind the paywall prompt.
  assert.equal(nextPhaseForRetry('purchase_required', retry), 'uploaded');
  assert.equal(nextPhaseForRetry('uploaded', retry), 'uploaded');
  const plan = classifyStartFailure({ status: 402, code: 'purchase_required' }, 1_000);
  assert.equal(nextPhaseForRetry('purchase_required', plan), 'purchase_required');
  assert.equal(nextPhaseForRetry('uploaded', plan), 'purchase_required');
  const gone = classifyStartFailure({ status: 404, code: 'not_found' }, 1_000);
  assert.equal(nextPhaseForRetry('purchase_required', gone), 'upload_failed');
});

test('the explicit sign_in_required code is an auth state: keep the job, never send them to the paywall', () => {
  const decision = classifyStartFailure({ status: 403, code: 'sign_in_required' });
  assert.equal(decision.phase, 'sign_in_required');
  assert.equal(decision.retrySameJob, true);
  assert.equal(decision.chooseAgain, false);
  assert.equal(decision.message, SIGN_IN_REQUIRED_COPY);
  // Never treated as the paywall draft-keeping case, and never a re-upload.
  assert.equal(shouldKeepDraft('sign_in_required'), false);
  assert.equal(nextPhaseForRetry('sign_in_required', decision), 'sign_in_required');
});

test('a 403 without the sign-in code is a generic refusal, not a missing account', () => {
  for (const error of [{ status: 403 }, { status: 403, code: 'forbidden' }, { status: 403, code: 'not_owner' }]) {
    const decision = classifyStartFailure(error, 5_000);
    assert.equal(decision.phase, 'uploaded', JSON.stringify(error));
    assert.equal(decision.retrySameJob, true);
    assert.equal(decision.chooseAgain, false);
    assert.ok(decision.message);
    // It must not claim the account is the problem.
    assert.ok(!/sign.?in|account/i.test(String(decision.message)), String(decision.message));
  }
  assert.equal(errorPrompt({ code: 'forbidden' }), null);
  assert.equal(errorPrompt({ code: 'purchase_required' }), 'plan');
  assert.equal(errorPrompt({ code: 'sign_in_required' }), 'account');
  assert.equal(errorPrompt({}), null);
  assert.equal(errorPrompt(null), null);
});

test('a reservation 409 is a wait state, not a paywall or a lost upload', () => {
  const decision = classifyStartFailure({ status: 409, code: 'free_analyses_in_progress' });
  assert.equal(decision.phase, 'uploaded');
  assert.equal(decision.retrySameJob, true);
  assert.equal(decision.chooseAgain, false);
  assert.equal(decision.message, ANALYSES_IN_PROGRESS_COPY);
  assert.equal(shouldKeepDraft('free_analyses_in_progress'), false);
  // The upload-expiry 409 keeps its own meaning.
  assert.equal(classifyStartFailure({ status: 409, code: 'upload_missing' }, UPLOAD_MISSING_AFTER_MS + 1).phase, 'upload_failed');
  // Once the month frees up, a retryable 409 releases any stale marker.
  assert.equal(nextPhaseForRetry('sign_in_required', decision), 'uploaded');
  assert.equal(nextPhaseForRetry('uploaded', decision), 'uploaded');
});
