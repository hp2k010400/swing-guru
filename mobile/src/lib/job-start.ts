// What to do when /start does not succeed, as pure decisions (tested in mobile/tests).
//
// The upload has already finished by the time /start is called, so most failures must *not* throw
// the bytes away: the job still holds them and the same job can be started again. Only a genuinely
// missing upload (or one past its server-side TTL) needs the video chosen again.

import { ANALYSES_IN_PROGRESS_COPY, SIGN_IN_REQUIRED_COPY } from './billing-core';

export type StartPhase = 'uploaded' | 'purchase_required' | 'sign_in_required' | 'upload_failed';

export interface StartFailure {
  phase: StartPhase;
  /** The job holds the uploaded bytes: retry /start for this same job (no re-upload). */
  retrySameJob: boolean;
  /** The golfer has to pick the video again. */
  chooseAgain: boolean;
  message: string | null;
}

/** Mirrors SG_BILLING/awaiting-upload janitor: an unstarted upload is discarded after this. */
export const UPLOAD_TTL_HOURS = 24;
/** How long an upload the server doesn't have yet may still be arriving (background session, slow network). */
export const UPLOAD_MISSING_AFTER_MS = 30 * 60_000;

export interface StartErrorLike {
  status?: number;
  code?: string;
}

export function classifyStartFailure(error: StartErrorLike | null | undefined, ageMs = 0): StartFailure {
  const code = error?.code ?? '';
  const status = error?.status ?? 0;
  // The server wants a plan before it will queue the job: keep the job, ask for a plan.
  if (code === 'purchase_required' || status === 402) {
    return { phase: 'purchase_required', retrySameJob: true, chooseAgain: false, message: null };
  }
  // Free analyses need an account that survives a reinstall - but only the server's explicit
  // `sign_in_required` code says so. That is an auth state, so it must never look like a paywall: the
  // uploaded bytes stay and the same job can be started once the account exists.
  if (code === 'sign_in_required') {
    return { phase: 'sign_in_required', retrySameJob: true, chooseAgain: false, message: SIGN_IN_REQUIRED_COPY };
  }
  // Any other 403 is a refusal (ownership, proxy, WAF), not a missing account: keep the upload and
  // retry the same job without claiming anything about signing in.
  if (status === 403) {
    return {
      phase: 'uploaded', retrySameJob: true, chooseAgain: false,
      message: "We couldn't start this analysis just now. We'll try again shortly.",
    };
  }
  // The bytes are safely uploaded; we just could not reach the service (or it could not check
  // the plan). Keep retrying this same job rather than making the golfer upload again.
  if (status === 0 || code === 'offline' || code === 'billing_unavailable' || status === 503 || status >= 500) {
    return {
      phase: 'uploaded', retrySameJob: true, chooseAgain: false,
      message: status === 0 || code === 'offline'
        ? "You're offline. We'll start this analysis when you're back."
        : "We couldn't check your plan just now. We'll retry shortly.",
    };
  }
  // The month's free slots are held by analyses that have not finished: wait, do not buy.
  if (code === 'free_analyses_in_progress') {
    return { phase: 'uploaded', retrySameJob: true, chooseAgain: false, message: ANALYSES_IN_PROGRESS_COPY };
  }
  if (code === 'upload_missing') {
    // /start is only asked once no upload is running in the app (analysis.ts), but an iOS background
    // upload can still be finishing after an app restart, and a large video on a slow connection
    // takes minutes. Only after UPLOAD_MISSING_AFTER_MS is it genuinely lost - which is still not the
    // 24-hour TTL, so say only what we know. (It used to be 60 s, which marked slow uploads failed.)
    if (ageMs > UPLOAD_MISSING_AFTER_MS) {
      return {
        phase: 'upload_failed', retrySameJob: false, chooseAgain: true,
        message: "We couldn't find your upload. Please choose the video again.",
      };
    }
    return { phase: 'uploaded', retrySameJob: true, chooseAgain: false, message: null };
  }
  if (status === 404 || code === 'not_found') {
    return {
      phase: 'upload_failed', retrySameJob: false, chooseAgain: true,
      message: `This upload is no longer available (uploads expire after ${UPLOAD_TTL_HOURS} hours). Please choose the video again.`,
    };
  }
  return {
    phase: 'upload_failed', retrySameJob: false, chooseAgain: true,
    message: "Your video didn't finish uploading. Please try again.",
  };
}

/** The trimmed draft is only kept when the server needs a plan before it will start the job. */
export function shouldKeepDraft(code: string | undefined): boolean {
  return errorPrompt({ code }) === 'plan';
}

/**
 * Which prompt a create/start failure asks for, from the server's documented codes only.
 *
 * A bare 403 is deliberately not "account": ownership/proxy refusals are not a missing durable
 * sign-in, and the golfer must not be told to sign in for them.
 */
export function errorPrompt(error: { code?: string } | null | undefined): 'plan' | 'account' | null {
  if (error?.code === 'purchase_required') return 'plan';
  if (error?.code === 'sign_in_required') return 'account';
  return null;
}

/**
 * The phase to store after a failed /start.
 *
 * A retryable failure (`uploaded`) must also release a previous `purchase_required` marker: once a
 * plan exists the job should start from the polling loop, not sit behind the paywall prompt. The
 * same applies to a `sign_in_required` marker. A 402 returns to `purchase_required`, a 403 to
 * `sign_in_required`, and a terminal failure stays failed.
 */
export function nextPhaseForRetry(current: StartPhase | 'uploading', failure: StartFailure): StartPhase {
  if (failure.phase !== 'uploaded') return failure.phase;
  // A retryable failure keeps the job in flight and releases a stale paywall marker.
  return current === 'upload_failed' ? 'upload_failed' : 'uploaded';
}
