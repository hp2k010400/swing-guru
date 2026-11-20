// Keeping the phone's list of in-flight uploads honest against the server (pure; unit-tested).
//
// The phone remembers each upload it started (analysis.ts `pending`) so nothing is lost across
// backgrounding or restarts. That memory can go stale: an upload marked "stopped" whose bytes in fact
// arrived (an iOS background session finishing after the app was closed), or a swing analysed while
// the golfer was elsewhere. The server's job status is the truth; this decides what to do with each
// remembered entry.
import type { JobStatus } from './types';

export type LocalPhase = 'uploading' | 'uploaded' | 'upload_failed' | 'purchase_required' | 'sign_in_required' | 'analysis_failed';

export type ReconcileAction =
  | { kind: 'keep' }                                   // nothing to change
  | { kind: 'remove' }                                 // finished (or gone): drop the entry
  | { kind: 'set'; phase: LocalPhase }                 // the server says something else
  | { kind: 'start' };                                 // bytes may be there: try /start for this job

export function reconcileAction(
  local: { phase: LocalPhase },
  server: { status: JobStatus } | 'missing',
  uploadRunning: boolean,
): ReconcileAction {
  // An upload still running in this app session owns its entry; asking the server now would only
  // ever see a partial video.
  if (uploadRunning) return { kind: 'keep' };
  if (server === 'missing') return { kind: 'remove' };              // job no longer exists (expired / deleted)
  switch (server.status) {
    case 'complete':
      return { kind: 'remove' };                                     // it's in the Library now
    case 'failed':
      return local.phase === 'analysis_failed' ? { kind: 'keep' } : { kind: 'set', phase: 'analysis_failed' };
    case 'queued':
    case 'processing':
      return local.phase === 'uploaded' ? { kind: 'keep' } : { kind: 'set', phase: 'uploaded' };
    case 'awaiting_upload':
      // A plan / account prompt stays until the golfer acts; everything else tries to start the same
      // job: /start succeeds if the bytes arrived, and says upload_missing if they did not.
      if (local.phase === 'purchase_required' || local.phase === 'sign_in_required') return { kind: 'keep' };
      return { kind: 'start' };
    default:
      return { kind: 'keep' };
  }
}
