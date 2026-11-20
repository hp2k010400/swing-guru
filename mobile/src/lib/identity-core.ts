// Durable identity decisions, kept dependency-free so they can be tested directly.
//
// Every user starts as an anonymous Supabase user. Sign in with Apple is *linked* to that same user
// (supabase.auth.linkIdentity), so the user id - and with it the swings, the monthly free usage and
// the RevenueCat app user id - never changes. If that Apple ID already belongs to another Swing Guru
// account (typically: the app was reinstalled), linking is refused and the golfer is asked before the
// app switches to that existing account; the guest swings on this device are left as they were.

export type LinkOutcome =
  | 'linked'                  // the guest account now has Apple attached (same user id)
  | 'signed_in'               // was already a durable account; signed in with Apple
  | 'existing_account'        // this Apple ID already has an account: confirm before switching
  | 'switched'                // switched to the existing Apple account
  | 'link_incomplete'         // signed in, but the server could not store the Apple grant yet
  | 'cancelled'               // the golfer closed the Apple sheet
  | 'not_configured'          // the Supabase project does not allow Apple / manual linking yet
  | 'failed';

export interface AuthErrorLike {
  code?: string | null;
  status?: number | null;
  message?: string | null;
}

/** What a failed linkIdentity / signInWithIdToken call means for the flow. */
export function classifyAuthError(error: AuthErrorLike | null | undefined): Exclude<LinkOutcome, 'linked' | 'signed_in' | 'switched' | 'link_incomplete'> {
  const code = (error?.code ?? '').toLowerCase();
  const message = (error?.message ?? '').toLowerCase();
  if (code === 'err_request_canceled' || code === 'err_canceled' || message.includes('canceled') || message.includes('cancelled')) {
    return 'cancelled';
  }
  if (code === 'identity_already_exists' || message.includes('identity is already linked') || message.includes('already linked to another user')) {
    return 'existing_account';
  }
  if (code === 'manual_linking_disabled' || code === 'provider_disabled' || code === 'validation_failed' && message.includes('provider')
    || message.includes('unsupported provider') || message.includes('provider is not enabled') || message.includes('manual linking')) {
    return 'not_configured';
  }
  return 'failed';
}

/** The golfer-facing copy for each outcome (null = nothing to say). */
export function outcomeCopy(outcome: LinkOutcome): { title: string; body: string } | null {
  switch (outcome) {
    case 'linked':
      return { title: "You're signed in", body: 'Your swings and free analyses now stay with your Apple ID, even if you reinstall the app or change phone.' };
    case 'signed_in':
    case 'switched':
      return { title: 'Welcome back', body: 'Your Swing Guru account is restored on this iPhone.' };
    case 'existing_account':
      return {
        title: 'This Apple ID already has an account',
        body: 'Switch to it to see its swings. Swings analysed on this iPhone without signing in stay with the guest profile.',
      };
    case 'not_configured':
      return { title: 'Sign in with Apple is not available yet', body: 'Please try again later. Your swings are safe on this iPhone.' };
    case 'link_incomplete':
      return {
        title: 'Almost there',
        body: "You're signed in, but we couldn't set up account deletion. Tap Sign in with Apple once more to finish.",
      };
    case 'failed':
      return { title: "Couldn't sign in", body: 'Please try again in a moment.' };
    case 'cancelled':
      return null;
  }
}

export interface AccountView {
  userId: string | null;
  anonymous: boolean;
  provider: 'apple' | 'email' | null;
  email: string | null;
  /** Apple's stable user identifier for this account (never a token), or null. */
  appleUserId: string | null;
}

/** A display-safe summary of the Supabase user (no tokens). */
export function accountView(user: { id?: string; is_anonymous?: boolean; email?: string | null;
  identities?: { provider?: string; id?: string | null;
    identity_data?: { sub?: string | null } | null }[] | null } | null | undefined): AccountView {
  if (!user?.id) return { userId: null, anonymous: true, provider: null, email: null, appleUserId: null };
  const providers = (user.identities ?? []).map((i) => i.provider);
  const provider = providers.includes('apple') ? 'apple' : providers.includes('email') ? 'email' : null;
  const apple = (user.identities ?? []).find((identity) => identity.provider === 'apple');
  const appleUserId = apple ? (apple.identity_data?.sub ?? apple.id ?? null) : null;
  return { userId: user.id, anonymous: Boolean(user.is_anonymous) && provider === null, provider,
    email: user.email ?? null, appleUserId };
}
