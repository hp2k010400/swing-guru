// Pure decisions for the durable-account flows: registering the Apple grant and deleting the
// account. Kept dependency-free (no React Native, no SDK, no network) so the screens and the tests
// exercise the same rules.
//
// Two things this module is careful about:
//   * the Apple authorization code is passed straight through to the API and never stored on the
//     device or logged;
//   * a deletion is only reported as "revoked" when the server says Apple confirmed the revocation.
//     Apple's TN3194 fallback (no credential obtainable) deletes the data and asks the golfer to
//     stop using Sign in with Apple in iOS Settings - it must never read as a server revocation.

export interface AppleAuthorization {
  authorizationCode: string | null;
  nonce: string;
}

/** The exact body the backend accepts for an Apple grant. */
export interface AppleCredentialBody {
  authorization_code: string;
  nonce: string;
}

/** The body the backend accepts, or null when Apple did not return a usable code. */
export function appleCredentialBody(credential: AppleAuthorization): AppleCredentialBody | null {
  const authorization_code = credential.authorizationCode?.trim();
  const nonce = credential.nonce?.trim();
  if (!authorization_code || !nonce) return null;
  return { authorization_code, nonce };
}

/** How the Apple grant ended up being handled, exactly as the server reported it. */
export type AppleRevocation = 'revoked' | 'not_required' | 'manual_required';

export interface DeletionResult {
  status: 'deleted';
  apple_revocation: AppleRevocation;
}

export type DeletionOutcome =
  | 'deleted'
  | 'deleted_manual_apple'
  | 'already_deleted'
  | 'reauthorize'
  | 'retry'
  | 'failed';

/**
 * What the app should do next. `result` is the server's answer; `error` is the API error when the
 * request failed. A 403 `account_deleted` means someone (this device or another) already finished.
 */
export function deletionOutcomeFrom(
  result: DeletionResult | null | undefined,
  error?: { status?: number; code?: string } | null,
): DeletionOutcome {
  if (result?.status === 'deleted') {
    return result.apple_revocation === 'manual_required' ? 'deleted_manual_apple' : 'deleted';
  }
  const code = error?.code ?? '';
  if (error?.status === 403 && code === 'account_deleted') return 'already_deleted';
  if (code === 'apple_reauthorization_required') return 'reauthorize';
  if (code === 'delete_incomplete' || code === 'apple_unavailable' || code === 'temporarily_unavailable') {
    return 'retry';
  }
  return 'failed';
}

export interface DeletionCopy {
  title: string;
  body: string;
}

/** Golfer-facing copy, or null when there is nothing to say. */
export function deletionCopy(outcome: DeletionOutcome): DeletionCopy | null {
  switch (outcome) {
    case 'deleted':
      return { title: 'Account deleted', body: 'Your Swing Guru account and all of its swings are gone.' };
    case 'deleted_manual_apple':
      return {
        title: 'Account deleted',
        body: 'Your data is gone. To finish, open iOS Settings, tap your name, then Sign in with Apple, and choose Stop Using Apple ID for Swing Guru.',
      };
    case 'already_deleted':
      return { title: 'Account deleted', body: 'This account was already deleted. Please sign in again to start fresh.' };
    case 'reauthorize':
      return {
        title: 'One more step',
        body: 'For your security, sign in with Apple again and we will finish deleting your account.',
      };
    case 'retry':
      return {
        title: "Your account isn't deleted yet",
        body: 'Your account is still here and not fully deleted. Please delete your account again - it is safe to repeat.',
      };
    case 'failed':
      return { title: "Couldn't delete your account", body: 'Nothing more was removed. Please try again in a moment.' };
  }
}
