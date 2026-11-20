// The Sign in with Apple orchestration, with every touchpoint injected so the real decisions can be
// driven by controlled (often deferred) fakes in tests - the same pattern as billing-flow.ts.
//
// Guarantees, in one place:
//   * the owner is snapshotted *before* the Apple sheet opens and re-checked inside the serialised
//     section, so an owner change while the sheet was open cannot link the credential to the wrong
//     guest;
//   * a guest link keeps the guest's UUID (the swings, the monthly allowance and the RevenueCat user
//     id all hang off it) and is only reported as linked when Supabase shows a durable Apple identity
//     *and* the caller has stored the Apple grant;
//   * the grant is handed back to the caller for storage *outside* this queue: the API path acquires
//     the session token through the serialised accessor, and calling it while holding the queue would
//     deadlock;
//   * an unexpected switch (a link that lands on another UUID) is abandoned instead of continued;
//   * a delayed "switch to the existing account" confirmation is refused when the guest profile that
//     asked for it is no longer the signed-in one.
import { appleCredentialBody, type AppleCredentialBody } from './account-core';
import type { IdentitySnapshot } from './identity-gate';
import { classifyAuthError, type AccountView, type LinkOutcome } from './identity-core';

export interface AppleGrantInput {
  /** Apple's identity token, used only for the Supabase call. */
  token: string;
  /** Apple's short-lived authorization code, used only for the server call. */
  authorizationCode: string | null;
  nonce: string;
}

/** A verified Apple credential waiting for the golfer to confirm a switch to an existing account. */
export interface PendingSwitch {
  token: string;
  nonce: string;
  authorizationCode: string | null;
  /** The guest account that asked for the switch; a delayed confirmation must match it. */
  ownerId: string;
}

export interface FlowResult {
  outcome: LinkOutcome;
  pending?: PendingSwitch;
  /**
   * The grant to store, if the flow got that far. The caller stores it outside the transition queue.
   * ``ownerId`` is the account the grant belongs to, so a late registration cannot be sent as a
   * different golfer.
   */
  grant?: { ownerId: string; body: AppleCredentialBody };
}

export interface IdentityFlowDeps {
  /** Session transitions are serialised here (the real one is the session's transition queue). */
  serialise<T>(operation: () => Promise<T>): Promise<T>;
  /** Make sure a session exists (anonymous on first launch) before the Apple call. */
  ensureSession(): Promise<void>;
  session(): Promise<AccountView | null>;
  ownerId(): string | null;
  snapshot(): IdentitySnapshot;
  link(grant: AppleGrantInput): Promise<{ error?: unknown; userId?: string | null }>;
  signInWithIdToken(grant: AppleGrantInput): Promise<{ error?: unknown }>;
  refresh(): Promise<void>;
  /** Stop using a session the golfer did not choose (caches + RevenueCat + local sign-out). */
  abandon(): Promise<void>;
  /** Clear account-scoped state before switching to the confirmed existing account. */
  beforeSwitch(): Promise<void>;
}

export interface IdentityFlow {
  signIn(grant: AppleGrantInput, intent: IdentitySnapshot): Promise<FlowResult>;
  switchTo(pending: PendingSwitch): Promise<FlowResult>;
}

export function createIdentityFlow(deps: IdentityFlowDeps): IdentityFlow {
  function intentAllows(intent: IdentitySnapshot): boolean {
    if (intent.owner === null) return true;      // first run: the bootstrap creates the guest here
    const now = deps.snapshot();
    return now.owner === intent.owner && now.generation === intent.generation;
  }

  async function signIn(grant: AppleGrantInput, intent: IdentitySnapshot): Promise<FlowResult> {
    return deps.serialise(async () => {
      const body = appleCredentialBody({ authorizationCode: grant.authorizationCode, nonce: grant.nonce });
      if (!grant.token || !body) return { outcome: 'failed' as LinkOutcome };
      if (!intentAllows(intent)) {
        // The account changed while the Apple sheet was open: the credential belongs to the golfer
        // who started, not to whoever is signed in now.
        return { outcome: 'failed' as LinkOutcome };
      }
      try {
        await deps.ensureSession();
      } catch {
        return { outcome: 'failed' as LinkOutcome };
      }
      const current = await deps.session();
      if (current?.userId && current.anonymous) {
        const linked = await deps.link(grant);
        if (linked.error) {
          const outcome: LinkOutcome = classifyAuthError(linked.error as never);
          return outcome === 'existing_account'
            ? { outcome, pending: { token: grant.token, nonce: grant.nonce,
                                    authorizationCode: grant.authorizationCode, ownerId: current.userId } }
            : { outcome };
        }
        if (linked.userId !== current.userId) {
          // The link must keep the guest UUID; anything else would move ownership of the swings.
          await deps.abandon();
          return { outcome: 'link_incomplete' as LinkOutcome };
        }
        await deps.refresh();
        const after = await deps.session();
        if (after?.userId !== current.userId || after?.provider !== 'apple' || after.anonymous) {
          await deps.abandon();
          return { outcome: 'link_incomplete' as LinkOutcome };
        }
        return { outcome: 'linked', grant: { ownerId: current.userId, body } };
      }
      const signedIn = await deps.signInWithIdToken(grant);
      if (signedIn.error) return { outcome: classifyAuthError(signedIn.error as never) };
      const after = await deps.session();
      if (!after?.userId || after.provider !== 'apple') return { outcome: 'link_incomplete' as LinkOutcome };
      return { outcome: 'signed_in', grant: { ownerId: after.userId, body } };
    });
  }

  async function switchTo(pending: PendingSwitch): Promise<FlowResult> {
    return deps.serialise(async () => {
      // A delayed confirmation must not sign this device into an account the golfer never chose: the
      // guest profile that asked for the switch has to still be the one signed in.
      if (!pending.ownerId || deps.ownerId() !== pending.ownerId) return { outcome: 'failed' };
      await deps.beforeSwitch();
      const signedIn = await deps.signInWithIdToken({ token: pending.token, nonce: pending.nonce,
                                                       authorizationCode: pending.authorizationCode });
      if (signedIn.error) return { outcome: classifyAuthError(signedIn.error as never) };
      const after = await deps.session();
      if (!after?.userId) return { outcome: 'link_incomplete' };
      const body = appleCredentialBody({ authorizationCode: pending.authorizationCode, nonce: pending.nonce });
      if (!body) return { outcome: 'link_incomplete' };
      return { outcome: 'switched', grant: { ownerId: after.userId, body } };
    });
  }

  return { signIn, switchTo };
}
