// Flow-level unit tests for the billing decisions, run with node:test (no Jest/Metro needed):
//   npm test
//
// They cover the mobile paywall contract the plan calls out: store prices (never invented), the
// 14-day trial gate (eligibility AND a free fortnight), lifetime having no trial, cancellation vs
// pending, and the server-authoritative post-purchase states (active vs "verification pending").
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { test } from 'node:test';

import {
  ANALYSES_IN_PROGRESS_COPY, SIGN_IN_REQUIRED_COPY, eligibilityOf, freeLeftCopy, kindOf, offerFromPackage,
  offersFromPackages, paywallReady, planNote, priceLabel, purchaseErrorState, reconcile, resetCopy,
  trialDisclosure, trialFor, type PackageLike,
} from '../src/lib/billing-core.ts';
import type { BillingStatus } from '../src/lib/types.ts';

function product(overrides: Partial<PackageLike['product']> = {}): PackageLike['product'] {
  return {
    identifier: 'com.swingguru.app.pro.monthly',
    title: 'Swing Guru Pro',
    priceString: '£9.99',
    introPrice: null,
    ...overrides,
  };
}

function pkg(overrides: Partial<PackageLike> = {}): PackageLike {
  return { identifier: '$rc_monthly', packageType: 'MONTHLY', product: product(), ...overrides };
}

const freeFortnight = { price: 0, priceString: '£0.00', periodUnit: 'WEEK', periodNumberOfUnits: 2 };

function status(overrides: Partial<BillingStatus> = {}): BillingStatus {
  return {
    api_version: 'v1', enabled: true, access: 'free', free_total: 3, free_used: 0, free_reserved: 0,
    free_remaining: 3, can_analyse: true, entitlement_active: false, product_id: null, expires_at: null,
    management_url: null, paid_allowance: 'unlimited',
    requires_sign_in: false, allowance_period_start: '2026-10-01T00:00:00+00:00',
    allowance_resets_at: '2026-11-01T00:00:00+00:00', allowance_timezone: 'UTC',
    allowance_model: 'calendar_month', ...overrides,
  };
}

test('package types map to the three plans this paywall sells', () => {
  assert.equal(kindOf('MONTHLY'), 'monthly');
  assert.equal(kindOf('ANNUAL'), 'yearly');
  assert.equal(kindOf('LIFETIME'), 'lifetime');
  assert.equal(kindOf('WEEKLY'), 'other');
});

test('unknown intro eligibility is never treated as eligible', () => {
  assert.equal(eligibilityOf(2), 'eligible');
  assert.equal(eligibilityOf(1), 'ineligible');
  assert.equal(eligibilityOf(3), 'no_offer');
  assert.equal(eligibilityOf(0), 'unknown');
  assert.equal(eligibilityOf(undefined), 'unknown');
});

test('a trial is shown only for an eligible, genuinely free fortnight', () => {
  assert.deepEqual(trialFor(product({ introPrice: freeFortnight }), 'eligible'), { days: 14, priceString: '£0.00' });
  assert.equal(trialFor(product({ introPrice: freeFortnight }), 'ineligible'), null);
  assert.equal(trialFor(product({ introPrice: freeFortnight }), 'unknown'), null);
  assert.equal(trialFor(product({ introPrice: freeFortnight }), 'no_offer'), null);
  assert.equal(trialFor(product(), 'eligible'), null);
  // A discounted (paid) intro is not a free trial; the length is the App Store's own (1.0.8).
  assert.equal(trialFor(product({ introPrice: { ...freeFortnight, price: 1.99, priceString: '£1.99' } }), 'eligible'), null);
  assert.equal(trialFor(product({ introPrice: { ...freeFortnight, periodNumberOfUnits: 1 } }), 'eligible')?.days, 7);
  assert.equal(trialFor(product({ introPrice: { ...freeFortnight, periodUnit: 'DAY', periodNumberOfUnits: 7 } }), 'eligible')?.days, 7);
  assert.equal(trialFor(product({ introPrice: { ...freeFortnight, periodUnit: 'DAY', periodNumberOfUnits: 3 } }), 'eligible')?.days, 3);
  assert.equal(trialFor(product({ introPrice: { ...freeFortnight, periodUnit: 'MONTH' } }), 'eligible'), null);
  assert.equal(trialFor(product({ introPrice: { ...freeFortnight, periodNumberOfUnits: 0 } }), 'eligible'), null);
  // A 14-day trial expressed in days is just as legitimate as two weeks.
  assert.deepEqual(
    trialFor(product({ introPrice: { ...freeFortnight, periodUnit: 'DAY', periodNumberOfUnits: 14 } }), 'eligible'),
    { days: 14, priceString: '£0.00' },
  );
  // A multi-period discount is not the single free fortnight.
  assert.equal(trialFor(product({ introPrice: { ...freeFortnight, cycles: 3 } }), 'eligible'), null);
  assert.equal(trialFor(product({ introPrice: { ...freeFortnight, cycles: 1 } }), 'eligible')?.days, 14);
});

test('a plan is only sellable when the store gave a localized price string', () => {
  assert.equal(offerFromPackage(pkg({ product: product({ priceString: '' }) }), 'unknown'), null);
  const offer = offerFromPackage(pkg(), 'unknown');
  assert.equal(offer?.priceString, '£9.99');
  assert.equal(offer?.renewalCopy, 'Then £9.99 per month, auto-renewing');
  assert.equal(offer?.trial, null);
});

test('lifetime is a one-time purchase with no trial and no renewal copy', () => {
  const offer = offerFromPackage(pkg({ identifier: '$rc_lifetime', packageType: 'LIFETIME',
    product: product({ identifier: 'com.swingguru.app.pro.lifetime', priceString: '£149.99', introPrice: freeFortnight }) }),
    'eligible');
  assert.equal(offer?.kind, 'lifetime');
  assert.equal(offer?.trial, null);
  assert.equal(offer?.renewalCopy, null);
  assert.equal(offer && priceLabel(offer), '£149.99 once');
});

test('offers keep the plan order and mark the yearly plan', () => {
  const offers = offersFromPackages([
    pkg({ identifier: '$rc_lifetime', packageType: 'LIFETIME',
      product: product({ identifier: 'com.swingguru.app.pro.lifetime', priceString: '£149.99', productType: 'NON_CONSUMABLE' }) }),
    pkg({ identifier: '$rc_annual', packageType: 'ANNUAL',
      product: product({ identifier: 'com.swingguru.app.pro.yearly', priceString: '£39.99', subscriptionPeriod: 'P1Y' }) }),
    pkg({ identifier: '$rc_monthly', packageType: 'MONTHLY',
      product: product({ identifier: 'com.swingguru.app.pro.monthly', priceString: '£9.99', subscriptionPeriod: 'P1M', introPrice: freeFortnight }) }),
    pkg({ identifier: '$rc_weekly', packageType: 'WEEKLY', product: product({ identifier: 'weekly', priceString: '£2.99' }) }),
  ], { 'com.swingguru.app.pro.monthly': 2, 'com.swingguru.app.pro.yearly': 2 });
  // Yearly leads, lifetime second, monthly last.
  assert.deepEqual(offers.map((o) => o.kind), ['yearly', 'lifetime', 'monthly']);
  assert.equal(offers[2].trial?.days, 14);
  assert.equal(offers[0].badge, 'Best value');
  assert.equal(offers[1].badge, 'Pay once');
  assert.equal(offers[2].badge, null);
  assert.equal(offers[0].renewalCopy, 'Then £39.99 per year, auto-renewing');
  assert.ok(paywallReady(offers));
  assert.equal(paywallReady([]), false);
  assert.equal(offersFromPackages([], {}).length, 0);
});

test('plan notes and disclosures are built from the store price', () => {
  const trial = offerFromPackage(pkg({ product: product({ subscriptionPeriod: 'P1M', introPrice: freeFortnight }) }), 'eligible');
  assert.ok(trial);
  assert.equal(planNote(trial), '14-day free trial available');
  assert.match(trialDisclosure(trial), /Free for 14 days, then £9\.99 per month/);
  assert.match(trialDisclosure(trial), /Cancel any time/);
  const unknown = offerFromPackage(pkg({ product: product({ subscriptionPeriod: 'P1M' }) }), 'unknown');
  assert.ok(unknown);
  assert.equal(planNote(unknown), 'Trial eligibility not confirmed');
  assert.match(trialDisclosure(unknown), /£9\.99 per month, auto-renewing/);
  assert.ok(!/trial/i.test(trialDisclosure(unknown)));
});

test('an unrecognised product, period or type is never offered', () => {
  const accepted = { monthly: 'com.swingguru.app.pro.monthly', yearly: 'com.swingguru.app.pro.yearly', lifetime: 'com.swingguru.app.pro.lifetime' };
  // Wrong product identifier for the package kind.
  assert.equal(offerFromPackage(pkg({ packageType: 'ANNUAL' }), 'eligible', accepted), null);
  // Right product, wrong period.
  assert.equal(offerFromPackage(pkg({ packageType: 'ANNUAL',
    product: product({ identifier: accepted.yearly, subscriptionPeriod: 'P1M' }) }), 'eligible', accepted), null);
  // A lifetime package that is actually a consumable.
  assert.equal(offerFromPackage(pkg({ packageType: 'LIFETIME',
    product: product({ identifier: accepted.lifetime, priceString: '£149.99', productType: 'CONSUMABLE' }) }),
    'eligible', accepted), null);
  // The genuine three are accepted.
  assert.ok(offerFromPackage(pkg(), 'eligible', accepted));
  assert.ok(offerFromPackage(pkg({ packageType: 'ANNUAL',
    product: product({ identifier: accepted.yearly, subscriptionPeriod: 'P1Y' }) }), 'eligible', accepted));
  assert.ok(offerFromPackage(pkg({ packageType: 'LIFETIME',
    product: product({ identifier: accepted.lifetime, priceString: '£149.99', productType: 'NON_CONSUMABLE' }) }),
    'eligible', accepted));
});

test('cancelled, pending and failed purchases are three different things', () => {
  assert.deepEqual(purchaseErrorState({ code: '1' }), { cancelled: true, pending: false, message: null });
  assert.deepEqual(purchaseErrorState({ userCancelled: true }), { cancelled: true, pending: false, message: null });
  const pending = purchaseErrorState({ code: '20' });
  assert.equal(pending.cancelled, false);
  assert.equal(pending.pending, true);
  const failed = purchaseErrorState({ code: '5' });
  assert.equal(failed.cancelled, false);
  assert.equal(failed.pending, false);
  // An unknown failure says nothing about whether the store charged the customer.
  assert.ok(!/charged/i.test(String(failed.message)));
  assert.match(String(failed.message), /try again/i);
});

test('the server decides access; a fresh SDK entitlement alone is pending', () => {
  assert.equal(reconcile(status({ access: 'pro', entitlement_active: true }), false), 'active');
  assert.equal(reconcile(status({ access: 'free', free_remaining: 2 }), true), 'pending');
  assert.equal(reconcile(status({ access: 'free', free_remaining: 2 }), false), 'free');
  assert.equal(reconcile(status({ access: 'exhausted', free_remaining: 0 }), false), 'exhausted');
  assert.equal(reconcile(status({ enabled: false, access: 'disabled' }), false), 'disabled');
  assert.equal(reconcile(null, true), 'unknown');
  // Billing off keeps everything usable.
  assert.equal(reconcile(status({ enabled: false, access: 'disabled', can_analyse: true }), false), 'disabled');
});

test('the remaining free count is phrased honestly', () => {
  assert.equal(freeLeftCopy(status({ free_remaining: 3 })), '3 free analyses left this month');
  assert.equal(freeLeftCopy(status({ free_remaining: 1, free_used: 2 })), 'Your last free analysis this month');
  assert.equal(freeLeftCopy(status({ free_remaining: 0 })), null);
  assert.equal(freeLeftCopy(status({ access: 'pro', entitlement_active: true })), null);
  assert.equal(freeLeftCopy(status({ enabled: false, access: 'disabled' })), null);
  assert.equal(freeLeftCopy(null), null);
});

test('the allowance is described as a calendar month with a reset date', () => {
  // The reset is the server's UTC instant, rendered in the golfer's own locale.
  assert.match(String(resetCopy(status())), /^Free analyses reset on /);
  assert.ok(!/January|October/.test(String(resetCopy(status({ allowance_resets_at: null })))));
  assert.equal(resetCopy(status({ allowance_resets_at: null })), null);
  assert.equal(resetCopy(status({ access: 'pro', entitlement_active: true })), null);
  assert.equal(resetCopy(status({ enabled: false, access: 'disabled' })), null);
  assert.equal(resetCopy(null), null);
});

test('an anonymous free golfer is asked to sign in, never to buy', () => {
  const anonymous = status({ access: 'sign_in_required', requires_sign_in: true, can_analyse: false });
  assert.equal(reconcile(anonymous, false), 'sign_in_required');
  // A purchase the store already granted is still only "pending" verification.
  assert.equal(reconcile(anonymous, true), 'pending');
  assert.equal(freeLeftCopy(anonymous), null);
  assert.match(SIGN_IN_REQUIRED_COPY, /sign in with apple/i);
});

test('slots held by unfinished analyses read as a wait, not a paywall', () => {
  const waiting = status({ access: 'in_progress', free_remaining: 0, can_analyse: false });
  assert.equal(reconcile(waiting, false), 'in_progress');
  assert.equal(freeLeftCopy(waiting), null);
  assert.match(ANALYSES_IN_PROGRESS_COPY, /still finishing/i);
});

// ---------------------------------------------------------------- value lines (Phase 4)

const priced = (kind: 'monthly' | 'yearly' | 'lifetime', price: number | undefined, currencyCode = 'GBP') => pkg({
  identifier: `$rc_${kind}`, packageType: kind === 'yearly' ? 'ANNUAL' : kind.toUpperCase(),
  product: product({
    identifier: `com.swingguru.app.pro.${kind}`, priceString: price === undefined ? '£?' : `£${price}`, price, currencyCode,
    ...(kind === 'lifetime' ? { productType: 'NON_CONSUMABLE' } : { subscriptionPeriod: kind === 'yearly' ? 'P1Y' : 'P1M' }),
  }),
});

test('the yearly value line is the real maths from the store prices, never overstated', () => {
  const offers = offersFromPackages([priced('monthly', 9.99), priced('yearly', 39.99), priced('lifetime', 149.99)], {});
  const yearly = offers.find((o) => o.kind === 'yearly')!;
  // 12 x 9.99 = 119.88; saving 79.89 = 66.64 % -> 66 % (rounded down). 1.0.8: no amount, no per-month line.
  assert.equal(yearly.value, 'Save 66% vs monthly');
  assert.equal(offers.find((o) => o.kind === 'lifetime')!.value, 'Pay once, keep Pro forever. No subscription.');
  assert.equal(offers.find((o) => o.kind === 'monthly')!.value, 'Flexible. Cancel any time.');
});

test('no saving is claimed without both real prices in one currency', () => {
  const noMonthly = offersFromPackages([priced('yearly', 39.99)], {}).find((o) => o.kind === 'yearly')!;
  assert.equal(noMonthly.value, '£3.33 a month, billed yearly.');
  const mixed = offersFromPackages([priced('monthly', 9.99, 'USD'), priced('yearly', 39.99)], {}).find((o) => o.kind === 'yearly')!;
  assert.doesNotMatch(mixed.value ?? '', /Save/);
  const unknown = offersFromPackages([priced('monthly', 9.99), priced('yearly', undefined)], {}).find((o) => o.kind === 'yearly')!;
  assert.equal(unknown.value, null);
  const dearer = offersFromPackages([priced('monthly', 2), priced('yearly', 39.99)], {}).find((o) => o.kind === 'yearly')!;
  assert.doesNotMatch(dearer.value ?? '', /Save/);
});

test('paywall copy has no dashes used as punctuation', () => {
  const all = [
    ...offersFromPackages([priced('monthly', 9.99), priced('yearly', 39.99), priced('lifetime', 149.99)], {}).flatMap((o) => [o.value ?? '', trialDisclosure(o), planNote(o)]),
    SIGN_IN_REQUIRED_COPY,
  ];
  for (const text of all) assert.doesNotMatch(text, /—|–| - /, text);
});

test('prices and savings always come from the store currency, never a hardcoded one', () => {
  // A US storefront shows dollars, a UK storefront pounds: the same code, only the store data differs.
  // (The per-month line is the fallback when there is no monthly plan to compare against.)
  const us = offersFromPackages([priced('yearly', 39.99, 'USD')], {}).find((o) => o.kind === 'yearly')!;
  const uk = offersFromPackages([priced('yearly', 39.99, 'GBP')], {}).find((o) => o.kind === 'yearly')!;
  assert.match(us.value ?? '', /\$3\.33/);
  assert.match(uk.value ?? '', /£3\.33/);
  const usSave = offersFromPackages([priced('monthly', 9.99, 'USD'), priced('yearly', 59.99, 'USD')], {}).find((o) => o.kind === 'yearly')!;
  assert.equal(usSave.value, 'Save 49% vs monthly');
  // The source never writes a currency symbol or code into user-facing copy.
  const src = ['../src/lib/billing-core.ts', '../src/lib/paywall-view.ts', '../src/app/paywall.tsx']
    .map((f) => readFileSync(new URL(f, import.meta.url), 'utf8').split('\n').filter((l) => !/^\s*(\/\/|\/\*|\*)/.test(l)).join('\n'));
  for (const text of src) assert.doesNotMatch(text, /['"`][^'"`]*(?:\$\d|£|€|\bUSD\b|\bGBP\b)/);
});
