// The local half of an account teardown (sign-out, caches, RevenueCat identity), with the identity
// binding that makes it safe when it runs late.
//
// A revocation or deletion can complete after a *different* golfer has signed in on this device. The
// teardown therefore runs inside the serialised transition queue and re-checks the expected owner
// after every await, so a late event for account A can never sign out account B.
import type { IdentitySnapshot } from './identity-gate';

export interface TeardownDeps {
  snapshot(): IdentitySnapshot;
  serialise<T>(operation: () => Promise<T>): Promise<T>;
  forgetBillingIdentity(): Promise<void>;
  signOutLocally(): Promise<void>;
}

export interface AccountTeardown {
  /**
   * Tear down `expectedOwner`'s local session. Returns true when this call performed the sign-out,
   * false when the expected account is no longer the signed-in one (so nothing was touched).
   */
  run(expectedOwner: string | null): Promise<boolean>;
}

export function createAccountTeardown(deps: TeardownDeps): AccountTeardown {
  return {
    run(expectedOwner: string | null): Promise<boolean> {
      return deps.serialise(async () => {
        const owns = () => {
          const now = deps.snapshot();
          return expectedOwner === null ? true : now.owner === expectedOwner;
        };
        if (!owns()) return false;
        await deps.forgetBillingIdentity();
        // Re-check inside the queue: the await above is exactly where a replacement account can
        // appear, and signing *them* out would be worse than leaving a stale local cache.
        if (!owns()) return false;
        await deps.signOutLocally();
        return true;
      });
    },
  };
}
