// Pins the live RevenueCat configuration verified on 2026-10-05 with the public iOS SDK key:
// current offering "default" with $rc_monthly -> com.swingguru.app.pro.monthly,
// $rc_annual -> com.swingguru.app.pro.yearly, $rc_lifetime -> com.swingguru.app.pro.lifetime,
// entitlement "pro". The paywall must sell exactly these and refuse anything else.
import assert from 'node:assert/strict';
import { test } from 'node:test';

import { offersFromPackages, type PackageLike } from '../src/lib/billing-core.ts';

const freeFortnight = { price: 0, priceString: '£0.00', periodUnit: 'WEEK', periodNumberOfUnits: 2 };

function live(): PackageLike[] {
  return [
    { identifier: '$rc_monthly', packageType: 'MONTHLY',
      product: { identifier: 'com.swingguru.app.pro.monthly', title: 'Monthly', priceString: '£9.99',
                 subscriptionPeriod: 'P1M', introPrice: freeFortnight } },
    { identifier: '$rc_annual', packageType: 'ANNUAL',
      product: { identifier: 'com.swingguru.app.pro.yearly', title: 'Yearly', priceString: '£39.99',
                 subscriptionPeriod: 'P1Y', introPrice: freeFortnight } },
    { identifier: '$rc_lifetime', packageType: 'LIFETIME',
      product: { identifier: 'com.swingguru.app.pro.lifetime', title: 'Lifetime', priceString: '£149.99',
                 introPrice: null } },
  ];
}

test('the live default offering becomes monthly, yearly and lifetime with the real product ids', () => {
  const eligible = { 'com.swingguru.app.pro.monthly': 2, 'com.swingguru.app.pro.yearly': 2 };
  const offers = offersFromPackages(live(), eligible);
  assert.deepEqual(offers.map((o) => [o.id, o.kind]), [
    ['$rc_annual', 'yearly'], ['$rc_lifetime', 'lifetime'], ['$rc_monthly', 'monthly'],
  ]);
  const byKind = Object.fromEntries(offers.map((o) => [o.kind, o]));
  assert.equal(byKind.monthly.priceString, '£9.99');
  assert.equal(byKind.yearly.priceString, '£39.99');
  assert.equal(byKind.lifetime.priceString, '£149.99');
  assert.deepEqual(byKind.monthly.trial, { days: 14, priceString: '£0.00' }, 'monthly 14-day trial');
  assert.deepEqual(byKind.yearly.trial, { days: 14, priceString: '£0.00' }, 'yearly 14-day trial');
  assert.equal(byKind.lifetime.trial, null, 'lifetime is a one-time purchase with no trial');
  assert.equal(byKind.lifetime.period, null);
});

test('trial copy is never shown when StoreKit says the customer is not eligible', () => {
  const offers = offersFromPackages(live(), { 'com.swingguru.app.pro.monthly': 1, 'com.swingguru.app.pro.yearly': 1 });
  assert.ok(offers.filter((o) => o.kind !== 'lifetime').every((o) => o.trial === null));
});

test('a package pointing at any other product is refused, not sold', () => {
  const tampered = live();
  tampered[0] = { ...tampered[0], product: { ...tampered[0].product, identifier: 'com.swingguru.app.pro.monthly.old' } };
  const offers = offersFromPackages(tampered, {});
  assert.deepEqual(offers.map((o) => o.id), ['$rc_annual', '$rc_lifetime']);
});
