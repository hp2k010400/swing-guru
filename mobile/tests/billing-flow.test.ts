// Flow-level tests for the billing controller, driven by a mocked RevenueCat SDK and a mocked
// API. These cover what the pure copy helpers cannot: serialized identity changes, results that
// land after the account has changed, disabled builds, and the server-authoritative states after
// a purchase or restore. No real purchase is involved anywhere.
import assert from 'node:assert/strict';
import { test } from 'node:test';

import { createBillingFlow, settleState, type FlowCustomerInfo, type FlowPackage, type FlowSdk } from '../src/lib/billing-flow.ts';
import type { BillingStatus } from '../src/lib/types.ts';

const MONTHLY = 'com.swingguru.app.pro.monthly';

function tick(): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, 0));
}

function gate(): { promise: Promise<void>; release: () => void } {
  let release: () => void = () => undefined;
  const promise = new Promise<void>((resolve) => {
    release = resolve;
  });
  return { promise, release };
}

function status(overrides: Partial<BillingStatus> = {}): BillingStatus {
  return {
    api_version: 'v1', enabled: true, access: 'free', free_total: 3, free_used: 3, free_reserved: 0,
    free_remaining: 0, can_analyse: false, entitlement_active: false, product_id: null, expires_at: null,
    management_url: null, paid_allowance: 'unlimited',
    requires_sign_in: false, allowance_period_start: '2026-10-01T00:00:00+00:00',
    allowance_resets_at: '2026-11-01T00:00:00+00:00', allowance_timezone: 'UTC',
    allowance_model: 'calendar_month', ...overrides,
  };
}

function flowPackage(overrides: Partial<FlowPackage> = {}): FlowPackage {
  return {
    identifier: '$rc_monthly',
    packageType: 'MONTHLY',
    product: {
      identifier: MONTHLY, title: 'Swing Guru Pro', priceString: '£9.99',
      subscriptionPeriod: 'P1M', productType: 'AUTO_RENEWABLE_SUBSCRIPTION', introPrice: null,
    },
    raw: { identifier: '$rc_monthly' },
    ...overrides,
  };
}

class FakeSdk implements FlowSdk {
  configured = false;
  configureUser: string | null = null;
  calls: string[] = [];
  inFlight = 0;
  maxInFlight = 0;
  purchaseCount = 0;
  restoreCount = 0;
  logOutCount = 0;
  offerings: FlowPackage[] = [flowPackage()];
  eligibility: Record<string, { status?: number } | undefined> = { [MONTHLY]: 2 };
  active: Record<string, unknown> = { pro: {} };
  purchaseError: unknown = null;
  restoreError: unknown = null;
  offeringsError: unknown = null;
  purchaseGate: { promise: Promise<void>; release: () => void } | null = null;
  restoreGate: { promise: Promise<void>; release: () => void } | null = null;
  offeringsGate: { promise: Promise<void>; release: () => void } | null = null;
  isConfiguredGate: { promise: Promise<void>; release: () => void } | null = null;
  logInGate: { promise: Promise<void>; release: () => void } | null = null;
  info: FlowCustomerInfo = { entitlements: { active: {} } };

  private async enter<T>(name: string, work: () => Promise<T>): Promise<T> {
    this.calls.push(name);
    this.inFlight += 1;
    this.maxInFlight = Math.max(this.maxInFlight, this.inFlight);
    try {
      return await work();
    } finally {
      this.inFlight -= 1;
    }
  }

  isConfigured(): Promise<boolean> {
    return this.enter('isConfigured', async () => {
      if (this.isConfiguredGate) await this.isConfiguredGate.promise;
      return this.configured;
    });
  }

  setLogLevel(): Promise<void> {
    return this.enter('setLogLevel', async () => undefined);
  }

  configure(config: { apiKey: string; appUserID: string }): void {
    this.calls.push(`configure:${config.appUserID}`);
    this.configured = true;
    this.configureUser = config.appUserID;
  }

  logIn(appUserId: string) {
    return this.enter(`logIn:${appUserId}`, async () => {
      if (this.logInGate) await this.logInGate.promise;
      this.configureUser = appUserId;
      return { customerInfo: this.info };
    });
  }

  logOut() {
    return this.enter('logOut', async () => {
      this.logOutCount += 1;
      this.configureUser = null;
      return this.info;
    });
  }

  getOfferings() {
    return this.enter('getOfferings', async () => {
      if (this.offeringsError) throw this.offeringsError;
      if (this.offeringsGate) await this.offeringsGate.promise;
      return { current: { availablePackages: this.offerings } };
    });
  }

  purchasePackage(): Promise<{ customerInfo: FlowCustomerInfo }> {
    return this.enter('purchasePackage', async () => {
      this.purchaseCount += 1;
      if (this.purchaseGate) await this.purchaseGate.promise;
      if (this.purchaseError) throw this.purchaseError;
      return { customerInfo: { entitlements: { active: this.active } } };
    });
  }

  restorePurchases() {
    return this.enter('restorePurchases', async () => {
      this.restoreCount += 1;
      if (this.restoreGate) await this.restoreGate.promise;
      if (this.restoreError) throw this.restoreError;
      return { entitlements: { active: this.active } };
    });
  }

  checkTrialOrIntroductoryPriceEligibility() {
    return this.enter('eligibility', async () => this.eligibility);
  }
}

function harness(options: { supported?: boolean; status?: BillingStatus; syncError?: unknown } = {}) {
  const sdk = new FakeSdk();
  const server = {
    user: 'user-a',
    status: options.status ?? status(),
    syncError: options.syncError ?? null,
    syncGate: null as { promise: Promise<void>; release: () => void } | null,
    syncCount: 0,
    fetchError: null as unknown,
    fetchGate: null as { promise: Promise<void>; release: () => void } | null,
  };
  const flow = createBillingFlow({
    supported: options.supported ?? true,
    apiKey: 'appl_test',
    entitlementId: 'pro',
    accepted: { monthly: MONTHLY, yearly: 'com.swingguru.app.pro.yearly', lifetime: 'com.swingguru.app.pro.lifetime' },
    sdk: async () => sdk,
    currentUserId: async () => server.user,
    fetchStatus: async () => {
      if (server.fetchError) throw server.fetchError;
      if (server.fetchGate) await server.fetchGate.promise;
      return server.status;
    },
    sync: async () => {
      server.syncCount += 1;
      if (server.syncGate) await server.syncGate.promise;
      if (server.syncError) throw server.syncError;
      return server.status;
    },
  });
  return { flow, sdk, server };
}

test('concurrent configuration of the same user configures the SDK exactly once', async () => {
  const { flow, sdk } = harness();
  await Promise.all([flow.configureIdentity('user-a'), flow.configureIdentity('user-a'), flow.configureIdentity('user-a')]);
  assert.deepEqual(sdk.calls, ['isConfigured', 'setLogLevel', 'configure:user-a']);
  assert.equal(flow.configuredUser(), 'user-a');
  // Already configured for that user: another call is a no-op.
  await flow.configureIdentity('user-a');
  assert.equal(sdk.calls.length, 3);
});

test('identity changes are serialized: configure then logIn, never overlapping', async () => {
  const { flow, sdk } = harness();
  await Promise.all([flow.configureIdentity('user-a'), flow.configureIdentity('user-b')]);
  assert.deepEqual(sdk.calls, ['isConfigured', 'setLogLevel', 'configure:user-a', 'isConfigured', 'logIn:user-b']);
  assert.equal(flow.configuredUser(), 'user-b');
  assert.equal(sdk.maxInFlight, 1); // one RevenueCat operation at a time
});

test('a sign-out during configuration never claims the old identity', async () => {
  const { flow, sdk } = harness();
  sdk.isConfiguredGate = gate();
  const configuring = flow.configureIdentity('user-a');
  await tick();
  const forget = flow.forgetIdentity(); // bumps the generation while isConfigured is awaiting
  sdk.isConfiguredGate.release();
  await configuring;
  await forget;
  // The stale configure must not have configured or claimed the SDK for user-a.
  assert.equal(flow.configuredUser(), null);
  assert.equal(sdk.configureUser, null);
  assert.ok(!sdk.calls.includes('configure:user-a'));
});

test('an offering fetched for a previous account is neither returned nor purchasable', async () => {
  const { flow, sdk, server } = harness();
  sdk.offeringsGate = gate();
  const loading = flow.loadOffers('user-a');
  await tick();
  server.user = 'user-b';
  sdk.offeringsGate.release();
  const { offers, unavailable } = await loading;
  assert.deepEqual(offers, []);
  assert.equal(unavailable, null);
  const stale = await flow.purchasePlan('$rc_monthly');
  assert.equal(stale.outcome, 'failed');
  assert.equal(sdk.purchaseCount, 0);
});

test('a purchase is not started when the account changes while configuring it', async () => {
  const { flow, sdk, server } = harness();
  await flow.loadOffers('user-a'); // configured for user-a, packages loaded
  server.user = 'user-b';          // the session switched without the flow being told
  sdk.logInGate = gate();
  const purchase = flow.purchasePlan('$rc_monthly');
  await tick();                    // now inside logIn(user-b)
  server.user = 'user-c';
  sdk.logInGate.release();
  const result = await purchase;
  assert.equal(result.outcome, 'identity_changed');
  assert.equal(sdk.purchaseCount, 0);
});

test('settle never returns the status of a different account', async () => {
  const { flow, server } = harness();
  server.syncGate = gate();
  const settling = flow.settle('user-a', true);
  await tick();
  server.user = 'user-b';
  server.syncGate.release();
  const { state, status: leaked } = await settling;
  assert.equal(state, 'identity_changed');
  assert.equal(leaked, null);

  // And it does not even ask when the account has already changed.
  const before = await flow.settle('user-a', true);
  assert.equal(before.state, 'identity_changed');
  assert.equal(before.status, null);
  assert.equal(server.syncCount, 1);
});

test('a sign-out during a purchase discards the result instead of applying it', async () => {
  const { flow, sdk, server } = harness();
  await flow.loadOffers('user-a');
  sdk.purchaseGate = gate();
  const purchase = flow.purchasePlan('$rc_monthly');
  await tick();
  const forget = flow.forgetIdentity(); // bumps the generation immediately
  sdk.purchaseGate.release();
  const result = await purchase;
  await forget;
  assert.equal(result.outcome, 'identity_changed');
  assert.equal(server.syncCount, 0);
  assert.equal(sdk.logOutCount, 1);
  assert.equal(flow.configuredUser(), null);
  // The next user configures cleanly.
  await flow.configureIdentity('user-b');
  assert.equal(flow.configuredUser(), 'user-b');
});

test('a sign-out during a restore discards the result', async () => {
  const { flow, sdk } = harness();
  await flow.loadOffers('user-a');
  sdk.restoreGate = gate();
  const restore = flow.restorePlan();
  await tick();
  const forget = flow.forgetIdentity();
  sdk.restoreGate.release();
  const result = await restore;
  await forget;
  assert.equal(result.outcome, 'identity_changed');
  assert.equal(sdk.logOutCount, 1);
});

test('an identity change during the sync response is not applied', async () => {
  const { flow, sdk, server } = harness();
  await flow.loadOffers('user-a');
  const purchase = await flow.purchasePlan('$rc_monthly');
  assert.equal(purchase.outcome, 'purchased');
  assert.equal(purchase.entitled, true);
  server.syncGate = gate();
  const settling = flow.settle('user-a', purchase.entitled);
  await tick();
  server.user = 'user-b';
  server.syncGate.release();
  const { state, status: leaked } = await settling;
  assert.equal(state, 'identity_changed');
  assert.equal(leaked, null);
  assert.equal(sdk.purchaseCount, 1); // no second purchase was started
});

test('same-user sync is the only thing that makes a purchase active', async () => {
  const { flow, sdk, server } = harness();
  await flow.loadOffers('user-a');
  const purchase = await flow.purchasePlan('$rc_monthly');
  server.status = status({ access: 'pro', entitlement_active: true, free_remaining: 0 });
  const { state, status: serverStatus } = await flow.settle('user-a', purchase.entitled);
  assert.equal(state, 'active');
  assert.equal(serverStatus?.access, 'pro');
  assert.equal(server.syncCount, 1);
  assert.equal(sdk.purchaseCount, 1);
});

test('the SDK entitlement alone is pending until the server agrees', async () => {
  const { flow } = harness();
  await flow.loadOffers('user-a');
  const purchase = await flow.purchasePlan('$rc_monthly');
  // Server still says "free" (RevenueCat has not propagated yet).
  const { state } = await flow.settle('user-a', purchase.entitled);
  assert.equal(state, 'pending');
});

test('a sync failure is unavailable, not a decision', async () => {
  const { flow } = harness({ syncError: new Error('503') });
  await flow.loadOffers('user-a');
  const purchase = await flow.purchasePlan('$rc_monthly');
  const { state } = await flow.settle('user-a', purchase.entitled);
  assert.equal(state, 'unavailable');
  assert.equal(settleState('unknown'), 'unavailable');
});

test('a restore with no purchase and a free server is inactive, not active', async () => {
  const { flow, sdk } = harness({ status: status({ access: 'free', free_remaining: 2 }) });
  await flow.loadOffers('user-a');
  sdk.active = {}; // this Apple ID has nothing to restore
  const result = await flow.restorePlan();
  assert.equal(result.outcome, 'purchased');
  assert.equal(result.entitled, false);
  const { state } = await flow.settle('user-a', result.entitled);
  assert.equal(state, 'inactive');
});

test('a deferred (ask-to-buy) purchase is pending, and a cancellation is quiet', async () => {
  const { flow, sdk } = harness();
  await flow.loadOffers('user-a');
  sdk.purchaseError = { code: '20' };
  const pending = await flow.purchasePlan('$rc_monthly');
  assert.equal(pending.outcome, 'pending');
  sdk.purchaseError = { code: '1' };
  const cancelled = await flow.purchasePlan('$rc_monthly');
  assert.equal(cancelled.outcome, 'cancelled');
  assert.equal(cancelled.message, null);
});

test('a disabled build never touches the SDK', async () => {
  const { flow, sdk } = harness({ supported: false });
  const offers = await flow.loadOffers('user-a');
  assert.deepEqual(offers.offers, []);
  assert.match(String(offers.unavailable), /not available in this build/);
  const purchase = await flow.purchasePlan('$rc_monthly');
  assert.equal(purchase.outcome, 'unavailable');
  const restore = await flow.restorePlan();
  assert.equal(restore.outcome, 'unavailable');
  assert.deepEqual(sdk.calls, []);
});

test('only the three accepted products with the expected period are offered', async () => {
  const { flow, sdk } = harness();
  sdk.offerings = [
    flowPackage(),
    flowPackage({ identifier: '$rc_annual', packageType: 'ANNUAL',
      product: { identifier: 'com.swingguru.app.pro.yearly', title: 'Yearly', priceString: '£39.99', subscriptionPeriod: 'P1Y' } }),
    flowPackage({ identifier: '$rc_unknown', packageType: 'ANNUAL',
      product: { identifier: 'com.other.product', title: 'Other', priceString: '£19.99', subscriptionPeriod: 'P1Y' } }),
    flowPackage({ identifier: '$rc_wrongperiod', packageType: 'ANNUAL',
      product: { identifier: 'com.swingguru.app.pro.yearly', title: 'Wrong period', priceString: '£19.99', subscriptionPeriod: 'P1M' } }),
    flowPackage({ identifier: '$rc_consumable', packageType: 'LIFETIME',
      product: { identifier: 'com.swingguru.app.pro.lifetime', title: 'Lifetime', priceString: '£149.99', productType: 'CONSUMABLE' } }),
  ];
  const { offers } = await flow.loadOffers('user-a');
  assert.deepEqual(offers.map((o) => o.id), ['$rc_annual', '$rc_monthly']);
  // A dropped package is not purchasable either.
  const dropped = await flow.purchasePlan('$rc_unknown');
  assert.equal(dropped.outcome, 'failed');
  assert.equal(sdk.purchaseCount, 0);
});

test('offers are cleared when the store cannot be reached', async () => {
  const { flow, sdk } = harness();
  await flow.loadOffers('user-a');
  sdk.offeringsError = new Error('offline');
  const { offers, unavailable } = await flow.loadOffers('user-a');
  assert.deepEqual(offers, []);
  assert.ok(unavailable);
  const stale = await flow.purchasePlan('$rc_monthly');
  assert.equal(stale.outcome, 'failed');
  assert.equal(sdk.purchaseCount, 0);
});

test('status() is guarded by the session identity it started with', async () => {
  const { flow, server } = harness();
  server.status = status({ access: 'pro', entitlement_active: true });
  const ok = await flow.status();
  assert.equal(ok.access, 'pro');
  server.fetchGate = gate();
  const pending = flow.status();
  await tick();
  server.user = 'user-b';
  server.fetchGate.release();
  await assert.rejects(() => pending);
});
