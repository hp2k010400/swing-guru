// Stale upload banners (Build 16: "Upload stopped" above a swing that had in fact been analysed).
import assert from 'node:assert/strict';
import { test } from 'node:test';

import { reconcileAction } from '../src/lib/pending-reconcile.ts';

test('a stopped upload whose swing was analysed is cleared, not shown as stopped', () => {
  assert.deepEqual(reconcileAction({ phase: 'upload_failed' }, { status: 'complete' }, false), { kind: 'remove' });
});

test('an upload still running in the app is never touched', () => {
  for (const status of ['awaiting_upload', 'queued', 'complete', 'failed'] as const) {
    assert.deepEqual(reconcileAction({ phase: 'uploading' }, { status }, true), { kind: 'keep' });
  }
});

test('bytes that may have arrived in the background are started, never written off', () => {
  assert.deepEqual(reconcileAction({ phase: 'upload_failed' }, { status: 'awaiting_upload' }, false), { kind: 'start' });
  assert.deepEqual(reconcileAction({ phase: 'uploading' }, { status: 'awaiting_upload' }, false), { kind: 'start' });
});

test('plan and account prompts stay until the golfer acts', () => {
  assert.deepEqual(reconcileAction({ phase: 'purchase_required' }, { status: 'awaiting_upload' }, false), { kind: 'keep' });
  assert.deepEqual(reconcileAction({ phase: 'sign_in_required' }, { status: 'awaiting_upload' }, false), { kind: 'keep' });
});

test('a server-side analysis failure is shown as that, not as a stopped upload', () => {
  assert.deepEqual(reconcileAction({ phase: 'upload_failed' }, { status: 'failed' }, false), { kind: 'set', phase: 'analysis_failed' });
  assert.deepEqual(reconcileAction({ phase: 'analysis_failed' }, { status: 'failed' }, false), { kind: 'keep' });
});

test('queued or processing shows as analysing; an expired job is dropped', () => {
  assert.deepEqual(reconcileAction({ phase: 'upload_failed' }, { status: 'processing' }, false), { kind: 'set', phase: 'uploaded' });
  assert.deepEqual(reconcileAction({ phase: 'uploaded' }, { status: 'queued' }, false), { kind: 'keep' });
  assert.deepEqual(reconcileAction({ phase: 'upload_failed' }, 'missing', false), { kind: 'remove' });
});
