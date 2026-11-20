// The billing controller: identity, offerings, purchase and restore, with every RevenueCat
// operation serialized on one queue and every awaited result re-checked against the identity it
// started with. No React Native import here on purpose, so the flow can be driven by mocks in
// mobile/tests; lib/billing.ts supplies the real SDK, session and API.
//
// Two invariants make the flow safe:
//   * one operation at a time — configure, logOut, purchase and restore can never interleave;
//   * a generation counter — if the user signs out (or the identity changes) while an awaited
//     call is in flight, its result is discarded instead of being applied to the wrong account.

import {
  offersFromPackages, purchaseErrorState, reconcile,
  type AcceptedProducts, type Eligibility, type PlanOffer, type ProductLike,
} from './billing-core';
import type { BillingStatus } from './types';

export interface FlowCustomerInfo {
  entitlements?: { active?: Record<string, unknown> | undefined } | undefined;
}

/** A package as the flow sees it: the SDK object stays opaque in `raw`. */
export interface FlowPackage {
  identifier: string;
  packageType: string;
  product: ProductLike;
  raw: unknown;
}

export interface FlowSdk {
  isConfigured(): Promise<boolean>;
  setLogLevel(level: 'INFO'): Promise<void>;
  configure(config: { apiKey: string; appUserID: string }): void;
  logIn(appUserId: string): Promise<{ customerInfo: FlowCustomerInfo }>;
  logOut(): Promise<unknown>;
  getOfferings(): Promise<{ current: { availablePackages: FlowPackage[] } | null }>;
  purchasePackage(pkg: unknown): Promise<{ customerInfo: FlowCustomerInfo }>;
  restorePurchases(): Promise<FlowCustomerInfo>;
  checkTrialOrIntroductoryPriceEligibility(ids: string[]): Promise<Record<string, { status?: number } | undefined>>;
}

export interface FlowDeps {
  /** Build/platform capability: false means no store exists (web, Android, no SDK key). */
  supported: boolean;
  apiKey: string;
  entitlementId: string;
  accepted: AcceptedProducts;
  sdk(): Promise<FlowSdk>;
  currentUserId(): Promise<string>;
  fetchStatus(): Promise<BillingStatus>;
  sync(): Promise<BillingStatus>;
}

export interface OffersResult {
  offers: PlanOffer[];
  unavailable: string | null;
}

export type PurchaseOutcome = 'purchased' | 'cancelled' | 'pending' | 'failed' | 'identity_changed' | 'unavailable';

export interface PurchaseResult {
  outcome: PurchaseOutcome;
  /** What the SDK reports right now; the server still has the last word. */
  entitled: boolean;
  message: string | null;
}

/** What the paywall should do after a purchase/restore has been verified server-side. */
export type SettleState = 'active' | 'pending' | 'inactive' | 'sign_in_required' | 'unavailable'
  | 'identity_changed';

const IDENTITY_CHANGED: PurchaseResult = {
  outcome: 'identity_changed', entitled: false,
  message: 'Your account changed during that step. Please sign in again and restore purchases.',
};

const NOT_SUPPORTED: PurchaseResult = {
  outcome: 'unavailable', entitled: false,
  message: 'In-app purchases are not available in this build.',
};

function entitledIn(info: FlowCustomerInfo | null | undefined, entitlementId: string): boolean {
  return Boolean(info?.entitlements?.active?.[entitlementId]);
}

/** Map the server's own access state (plus the SDK's fresh view) to the paywall's next step. */
export function settleState(state: ReturnType<typeof reconcile>): SettleState {
  if (state === 'active') return 'active';
  if (state === 'pending') return 'pending';
  if (state === 'sign_in_required') return 'sign_in_required';
  if (state === 'unknown') return 'unavailable';
  return 'inactive';
}

export function createBillingFlow(deps: FlowDeps) {
  let queue: Promise<unknown> = Promise.resolve();
  let generation = 0;
  let configuredFor: string | null = null;
  const loadedPackages = new Map<string, FlowPackage>();

  /** Serialize every RevenueCat operation; failures never poison the chain. */
  function enqueue<T>(operation: () => Promise<T>): Promise<T> {
    const run = queue.then(operation, operation);
    queue = run.then(() => undefined, () => undefined);
    return run;
  }

  /**
   * Configure the SDK for `appUserId` on the queue. `startedAt` is the generation the caller was
   * working under: if a sign-out happened while we awaited the SDK, nothing is committed.
   */
  async function configureLocked(appUserId: string, startedAt: number): Promise<void> {
    if (!deps.supported || !appUserId || configuredFor === appUserId) return;
    const sdk = await deps.sdk();
    const alreadyConfigured = await sdk.isConfigured();
    if (startedAt !== generation) return; // signed out (or switched) while we waited
    // A configure/logIn for another user invalidates any offering we had cached for the old one.
    loadedPackages.clear();
    if (!alreadyConfigured) {
      await sdk.setLogLevel('INFO');
      sdk.configure({ apiKey: deps.apiKey, appUserID: appUserId });
    } else {
      await sdk.logIn(appUserId);
    }
    if (startedAt !== generation) {
      // The account changed during logIn/configure: do not claim this identity is ready.
      loadedPackages.clear();
      return;
    }
    configuredFor = appUserId;
  }

  /** Configure once per Supabase UUID. Concurrent callers share the one queued operation. */
  function configureIdentity(appUserId: string): Promise<void> {
    return enqueue(() => configureLocked(appUserId, generation));
  }

  /** Sign-out / account deletion: bump the generation first so in-flight results are discarded. */
  function forgetIdentity(): Promise<void> {
    generation += 1;
    configuredFor = null;
    loadedPackages.clear();
    return enqueue(async () => {
      try {
        if (!deps.supported) return;
        const sdk = await deps.sdk();
        if (await sdk.isConfigured()) await sdk.logOut();
      } catch {
        /* the local caches are already cleared */
      } finally {
        // A configure that was already awaiting the SDK may have run before this logout; never
        // leave `configuredFor` claiming an identity the SDK has just been logged out of.
        configuredFor = null;
        loadedPackages.clear();
      }
    });
  }

  async function loadOffers(appUserId: string): Promise<OffersResult> {
    if (!deps.supported) return { offers: [], unavailable: 'Plans are not available in this build.' };
    return enqueue(async () => {
      const startedAt = generation;
      loadedPackages.clear();
      try {
        await configureLocked(appUserId, startedAt);
        if (startedAt !== generation) return { offers: [], unavailable: null };
        const sdk = await deps.sdk();
        const offerings = await sdk.getOfferings();
        const packages = offerings.current?.availablePackages ?? [];
        if (!packages.length) {
          loadedPackages.clear();
          return { offers: [], unavailable: 'No plans are available right now. Please try again later.' };
        }
        const productIds = packages.map((pkg) => pkg.product.identifier);
        let eligibility: Record<string, number | undefined> = {};
        try {
          const map = await sdk.checkTrialOrIntroductoryPriceEligibility(productIds);
          eligibility = Object.fromEntries(Object.entries(map).map(([id, entry]) => [id, entry?.status]));
        } catch {
          eligibility = {}; // unknown eligibility -> plain purchase copy
        }
        // Final check, after every await, against both the generation and the real session: an
        // offering fetched for a previous account must never be cached or returned.
        let currentUser: string | null = null;
        try {
          currentUser = await deps.currentUserId();
        } catch {
          currentUser = null;
        }
        if (startedAt !== generation || currentUser !== appUserId) {
          loadedPackages.clear();
          return { offers: [], unavailable: null };
        }
        const offers = offersFromPackages(packages, eligibility, deps.accepted);
        loadedPackages.clear();
        for (const offer of offers) {
          const pkg = packages.find((candidate) => candidate.identifier === offer.id);
          if (pkg) loadedPackages.set(offer.id, pkg);
        }
        if (!offers.length) {
          return { offers: [], unavailable: 'No plans are available right now. Please try again later.' };
        }
        return { offers, unavailable: null };
      } catch {
        loadedPackages.clear();
        return { offers: [], unavailable: "We couldn't reach the App Store. Check your connection and try again." };
      }
    });
  }

  async function withIdentity(run: (expectedUser: string, sdk: FlowSdk) => Promise<PurchaseResult>): Promise<PurchaseResult> {
    if (!deps.supported) return NOT_SUPPORTED;
    return enqueue(async () => {
      const startedAt = generation;
      const expectedUser = await deps.currentUserId();
      if (startedAt !== generation) return IDENTITY_CHANGED;
      await configureLocked(expectedUser, startedAt);
      if (startedAt !== generation) return IDENTITY_CHANGED;
      const sdk = await deps.sdk();
      // Immediately before StoreKit: the SDK identity and the session must still agree.
      const beforeCall = await deps.currentUserId();
      if (startedAt !== generation || beforeCall !== expectedUser) return IDENTITY_CHANGED;
      const result = await run(expectedUser, sdk);
      // Immediately after StoreKit: never apply a result to a different account.
      let afterCall: string | null = null;
      try {
        afterCall = await deps.currentUserId();
      } catch {
        afterCall = null;
      }
      if (startedAt !== generation || afterCall !== expectedUser) return IDENTITY_CHANGED;
      return result;
    });
  }

  function purchasePlan(offerId: string): Promise<PurchaseResult> {
    return withIdentity(async (_expectedUser, sdk) => {
      const pkg = loadedPackages.get(offerId);
      if (!pkg) {
        return { outcome: 'failed', entitled: false, message: 'That plan is no longer available. Please refresh.' };
      }
      try {
        const { customerInfo } = await sdk.purchasePackage(pkg.raw);
        return { outcome: 'purchased', entitled: entitledIn(customerInfo, deps.entitlementId), message: null };
      } catch (error) {
        return classifySdkError(error);
      }
    });
  }

  function restorePlan(): Promise<PurchaseResult> {
    return withIdentity(async (_expectedUser, sdk) => {
      try {
        const info = await sdk.restorePurchases();
        return { outcome: 'purchased', entitled: entitledIn(info, deps.entitlementId), message: null };
      } catch (error) {
        return classifySdkError(error);
      }
    });
  }

  function classifySdkError(error: unknown): PurchaseResult {
    const state = purchaseErrorState((error ?? {}) as { code?: unknown; userCancelled?: unknown });
    if (state.cancelled) return { outcome: 'cancelled', entitled: false, message: null };
    if (state.pending) return { outcome: 'pending', entitled: false, message: state.message };
    return { outcome: 'failed', entitled: false, message: state.message };
  }

  /**
   * After a purchase/restore: ask the server to verify this same user, and only then decide.
   * A server that has not caught up yet is "pending", never "active".
   */
  async function settle(expectedUser: string, sdkEntitled: boolean): Promise<{ state: SettleState; status: BillingStatus | null }> {
    const startedAt = generation;
    // Check the identity before touching the server too: the whole point is not to ask about (or
    // apply) the wrong account.
    let before: string | null = null;
    try {
      before = await deps.currentUserId();
    } catch {
      before = null;
    }
    if (startedAt !== generation || before !== expectedUser) {
      return { state: 'identity_changed', status: null };
    }
    let status: BillingStatus;
    try {
      status = await deps.sync();
    } catch {
      return startedAt === generation
        ? { state: 'unavailable', status: null }
        : { state: 'identity_changed', status: null };
    }
    let after: string | null = null;
    try {
      after = await deps.currentUserId();
    } catch {
      after = null; // cannot confirm who we are: never apply this result
    }
    if (startedAt !== generation || after !== expectedUser) {
      // Never hand back the status of the wrong account.
      return { state: 'identity_changed', status: null };
    }
    return { state: settleState(reconcile(status, sdkEntitled)), status };
  }

  /**
   * A plain refresh. The expected identity is always the session's current user at call time, and
   * the answer is discarded if the account changes while the request is in flight.
   */
  async function status(): Promise<BillingStatus> {
    const startedAt = generation;
    let expectedUser: string | null = null;
    try {
      expectedUser = await deps.currentUserId();
    } catch {
      expectedUser = null;
    }
    if (expectedUser === null) throw new Error('identity unavailable');
    const next = await deps.fetchStatus();
    let after: string | null = null;
    try {
      after = await deps.currentUserId();
    } catch {
      after = null;
    }
    if (startedAt !== generation || after !== expectedUser) {
      throw new Error('identity changed');
    }
    return next;
  }

  return {
    configureIdentity,
    forgetIdentity,
    loadOffers,
    purchasePlan,
    restorePlan,
    settle,
    status,
    /** Test/diagnostics helper: the identity the SDK is configured for. */
    configuredUser: () => configuredFor,
  };
}

export type BillingFlow = ReturnType<typeof createBillingFlow>;

export type { Eligibility };
