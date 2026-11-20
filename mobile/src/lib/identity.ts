// Sign in with Apple, linked to the existing anonymous Supabase user.
//
// The decisions live in identity-flow.ts (dependency-injected and unit-tested); this file only
// supplies the real native SDK, the Supabase client, the API and the local teardown. Native only
// (iOS): the Apple sheet returns an identity token signed by Apple - Supabase verifies it against the
// hashed nonce we sent, so the token cannot be replayed into another session.
//
// The same sheet also returns a short-lived *authorization code*. After Supabase accepts the
// sign-in, the app hands that code (with the raw nonce) to our service once, so the server can
// exchange it for the golfer's Apple refresh token and revoke the grant when the account is deleted.
// The code is never stored on the device and never logged.
import * as AppleAuthentication from 'expo-apple-authentication';
import * as Crypto from 'expo-crypto';
import { useEffect, useState } from 'react';
import { AppState, Platform } from 'react-native';

import { api } from './api';
import { forgetBillingIdentity } from './billing';
import { createIdentityFlow, type AppleGrantInput, type PendingSwitch } from './identity-flow';
import { accountView, classifyAuthError, type AccountView, type LinkOutcome } from './identity-core';
import {
  accessTokenUnlocked, refreshSessionUnlocked, runAuthTransition, sessionIdentity, signOutLocally, supabase,
} from './session';
import { swingCache } from './swing-cache';

export type { PendingSwitch } from './identity-flow';

export async function appleSignInAvailable(): Promise<boolean> {
  if (Platform.OS !== 'ios' || !supabase) return false;
  try {
    return await AppleAuthentication.isAvailableAsync();
  } catch {
    return false;
  }
}

async function appleCredential(): Promise<AppleGrantInput | LinkOutcome> {
  const nonce = Crypto.randomUUID();
  const hashed = await Crypto.digestStringAsync(Crypto.CryptoDigestAlgorithm.SHA256, nonce);
  try {
    const credential = await AppleAuthentication.signInAsync({
      requestedScopes: [AppleAuthentication.AppleAuthenticationScope.EMAIL],
      nonce: hashed,
    });
    return credential.identityToken
      ? { token: credential.identityToken, authorizationCode: credential.authorizationCode, nonce }
      : 'failed';
  } catch (error) {
    return classifyAuthError(error as { code?: string; message?: string });
  }
}

/** A session the golfer did not choose: drop it locally instead of continuing as that account. */
async function abandonUnexpectedSession(): Promise<void> {
  await forgetBillingIdentity();
  await signOutLocally();
}

const flow = createIdentityFlow({
  serialise: runAuthTransition,
  ensureSession: async () => {
    // Unlocked on purpose: this runs inside the transition queue, where the serialised accessor
    // would deadlock waiting for itself.
    await accessTokenUnlocked();
  },
  session: async () => {
    if (!supabase) return null;
    const { data } = await supabase.auth.getSession();
    return accountView(data.session?.user);
  },
  ownerId: () => sessionIdentity().owner,
  snapshot: () => sessionIdentity(),
  link: async (grant) => {
    if (!supabase) return { error: { code: 'not_configured' } };
    const { data, error } = await supabase.auth.linkIdentity({
      provider: 'apple', token: grant.token, nonce: grant.nonce });
    return error ? { error } : { userId: (data as { user?: { id?: string } } | null)?.user?.id ?? null };
  },
  signInWithIdToken: async (grant) => {
    if (!supabase) return { error: { code: 'not_configured' } };
    const { error } = await supabase.auth.signInWithIdToken({
      provider: 'apple', token: grant.token, nonce: grant.nonce });
    return error ? { error } : {};
  },
  refresh: async () => {
    await refreshSessionUnlocked();
  },
  abandon: abandonUnexpectedSession,
  beforeSwitch: async () => {
    swingCache.clear();
    await forgetBillingIdentity();
  },
});

/**
 * Store the Apple grant after the transition queue has been released. The identity is re-checked
 * here: a registration that lands while a different golfer is signed in is refused, not sent as
 * them.
 */
async function storeGrant(result: { outcome: LinkOutcome; pending?: PendingSwitch;
                                    grant?: { ownerId: string; body: { authorization_code: string; nonce: string } } }) {
  if (!result.grant) return { outcome: result.outcome, pending: result.pending };
  if (sessionIdentity().owner !== result.grant.ownerId) return { outcome: 'link_incomplete' as LinkOutcome };
  try {
    await api.registerApple(result.grant.body);
  } catch {
    return { outcome: 'link_incomplete' as LinkOutcome };
  }
  return { outcome: result.outcome, pending: result.pending };
}

/** Attach Apple to the current guest account (same user id), or sign in if already durable. */
export async function signInWithApple(): Promise<{ outcome: LinkOutcome; pending?: PendingSwitch }> {
  if (!supabase) return { outcome: 'not_configured' };
  // Snapshot the owner *before* the sheet: the golfer can take a while, and the credential belongs
  // to whoever started this sign-in.
  const intent = sessionIdentity();
  const credential = await appleCredential();
  if (typeof credential === 'string') return { outcome: credential };
  return storeGrant(await flow.signIn(credential, intent));
}

/** After the golfer confirmed: leave the guest profile and sign in to the existing Apple account. */
export async function switchToAppleAccount(pending: PendingSwitch): Promise<LinkOutcome> {
  if (!supabase) return 'not_configured';
  return (await storeGrant(await flow.switchTo(pending))).outcome;
}

/**
 * A fresh Apple authorization for account deletion: only needed for an account linked before the
 * server stored refresh tokens. Returns null when the golfer cancels or Apple is unavailable, which
 * lets the caller offer Apple's manual fallback instead of blocking the deletion forever.
 */
export async function appleAuthorizationForDeletion(): Promise<{ authorization_code: string; nonce: string } | null> {
  const credential = await appleCredential();
  if (typeof credential === 'string') return null;
  const code = credential.authorizationCode?.trim();
  return code ? { authorization_code: code, nonce: credential.nonce } : null;
}

/** The signed-in account, updated on every auth change (no tokens exposed). */
export function useAccount(): AccountView {
  const [account, setAccount] = useState<AccountView>(() => accountView(null));
  useEffect(() => {
    if (!supabase) return;
    let alive = true;
    supabase.auth.getSession().then(({ data }) => alive && setAccount(accountView(data.session?.user)));
    const { data } = supabase.auth.onAuthStateChange((_event, session) => setAccount(accountView(session?.user)));
    return () => {
      alive = false;
      data.subscription.unsubscribe();
    };
  }, []);
  return account;
}

/**
 * Apple can revoke this app's credential (the golfer deleted their Apple ID or stopped using it
 * with Swing Guru). This device must then stop using the session instead of pretending it is valid.
 * iOS only: on other platforms, and on the simulator, the state check throws and is ignored.
 */
export function useAppleCredentialGuard(account: AccountView, onRevoked: () => void): void {
  useEffect(() => {
    if (Platform.OS !== 'ios' || !account.appleUserId) return;
    const appleUserId = account.appleUserId;
    const expectedUserId = account.userId;
    let alive = true;
    // Only act while the account whose credential was checked is *still* the signed-in one, so a
    // late event for account A can never sign out a replacement account B.
    const stillOurs = () => {
      const current = sessionIdentity().owner;
      return current !== null && current === expectedUserId;
    };
    const check = () => {
      AppleAuthentication.getCredentialStateAsync(appleUserId)
        .then((state) => {
          if (!alive || !stillOurs()) return;
          // Only the documented "no longer valid" states. TRANSFERRED means the credential moved to
          // another account/team; it must not sign this device out.
          if (state === AppleAuthentication.AppleAuthenticationCredentialState.REVOKED
              || state === AppleAuthentication.AppleAuthenticationCredentialState.NOT_FOUND) {
            onRevoked();
          }
        })
        .catch(() => undefined);
    };
    check();
    let subscription: { remove: () => void } | undefined;
    try {
      subscription = AppleAuthentication.addRevokeListener(() => {
        if (alive && stillOurs()) onRevoked();
      });
    } catch {
      /* the listener is unavailable on this OS version; the state check above still runs */
    }
    // Apple's listener is not guaranteed while the app is backgrounded; re-check on foreground.
    const appState = AppState.addEventListener('change', (state) => {
      if (state === 'active') check();
    });
    return () => {
      alive = false;
      subscription?.remove();
      appState.remove();
    };
  }, [account.appleUserId, account.userId, onRevoked]);
}
