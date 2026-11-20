// Paywall decisions: never sell a second plan to an active subscriber, always keep restore
// reachable, and never make a categorical trial claim StoreKit has not confirmed.
import assert from 'node:assert/strict';
import { test } from 'node:test';

import { offerFromPackage, type PlanOffer } from '../src/lib/billing-core.ts';
import { afterSettleCopy, ctaLabel, paywallView } from '../src/lib/paywall-view.ts';
import type { BillingStatus } from '../src/lib/types.ts';

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

function offer(identifier: string, packageType: string, eligibility = 2, intro = true): PlanOffer {
  return offerFromPackage({
    identifier: `$rc_${packageType.toLowerCase()}`,
    packageType,
    product: {
      identifier,
      title: packageType,
      priceString: packageType === 'LIFETIME' ? '£149.99' : packageType === 'ANNUAL' ? '£39.99' : '£9.99',
      subscriptionPeriod: packageType === 'MONTHLY' ? 'P1M' : packageType === 'ANNUAL' ? 'P1Y' : null,
      productType: packageType === 'LIFETIME' ? 'NON_CONSUMABLE' : 'AUTO_RENEWABLE_SUBSCRIPTION',
      introPrice: intro && packageType !== 'LIFETIME'
        ? { price: 0, priceString: '£0.00', periodUnit: 'WEEK', periodNumberOfUnits: 2, cycles: 1 }
        : null,
    },
  }, eligibility === 2 ? 'eligible' : eligibility === 1 ? 'ineligible' : 'unknown') as PlanOffer;
}

test('an active subscriber is never offered another purchase', () => {
  const view = paywallView(status({ access: 'pro', entitlement_active: true }), [offer('com.swingguru.app.pro.monthly', 'MONTHLY')], null, false);
  assert.equal(view.subscribed, true);
  assert.equal(view.showBuy, false);
  assert.equal(view.showPlans, false);
  assert.equal(view.showManage, true);
  assert.equal(view.showRestore, true);
  assert.equal(view.ctaLabel, 'Continue');
});

test('restore stays reachable when the offerings fail or are empty', () => {
  const view = paywallView(status(), [], null, false);
  assert.equal(view.showBuy, false);
  assert.equal(view.showPlans, false);
  assert.equal(view.showRestore, true); // the golfer can still try to restore
  assert.equal(view.showManage, false);
});

test('a trial is offered only for an eligible offer, and the copy is neutral otherwise', () => {
  const monthly = offer('com.swingguru.app.pro.monthly', 'MONTHLY', 2);
  const trialView = paywallView(status(), [monthly], monthly, false);
  assert.equal(trialView.ctaLabel, 'Start free trial');
  assert.match(String(trialView.note), /14-day free trial/);
  assert.match(String(trialView.disclosure), /then £9\.99 per month/);
  assert.match(String(trialView.disclosure), /auto-renew/);

  const unknown = offer('com.swingguru.app.pro.monthly', 'MONTHLY', 0);
  const unknownView = paywallView(status(), [unknown], unknown, false);
  assert.equal(unknownView.note, 'Trial eligibility not confirmed');
  assert.match(unknownView.ctaLabel, /^Subscribe/);

  const ineligible = offer('com.swingguru.app.pro.monthly', 'MONTHLY', 1);
  assert.equal(paywallView(status(), [ineligible], ineligible, false).note, 'No free trial');
});

test('lifetime has no trial copy and a one-time price', () => {
  const lifetime = offer('com.swingguru.app.pro.lifetime', 'LIFETIME');
  const view = paywallView(status(), [lifetime], lifetime, false);
  assert.equal(view.note, 'One-time purchase · no trial');
  assert.match(view.ctaLabel, /£149\.99/);
  assert.match(String(view.disclosure), /not a subscription/i);
  // The disclosure explicitly denies a trial rather than implying one.
  assert.match(String(view.disclosure), /no free trial/i);
});

test('the buy and restore buttons are disabled while an operation is running', () => {
  const monthly = offer('com.swingguru.app.pro.monthly', 'MONTHLY');
  const view = paywallView(status(), [monthly], monthly, true);
  assert.equal(view.showBuy, true); // still shown, but the screen disables it while busy
  assert.equal(view.showRestore, false);
  assert.equal(ctaLabel(monthly), 'Start free trial');
  assert.equal(ctaLabel(null), 'Choose a plan');
});

test('1.0.8: an eligible golfer on the US store sees "7 days free" on yearly, the price under it, and Start free trial', async () => {
  const { offersFromPackages, trialHeadline, trialPriceLine } = await import('../src/lib/billing-core.ts');
  const week = { price: 0, priceString: '$0.00', periodUnit: 'WEEK', periodNumberOfUnits: 1, cycles: 1 };
  const usd = (id: string, type: string, price: number, period: string) => ({
    identifier: `$rc_${type.toLowerCase()}`, packageType: type,
    product: { identifier: id, title: type === 'ANNUAL' ? 'Yearly' : 'Monthly', priceString: `$${price}`, price,
      currencyCode: 'USD', subscriptionPeriod: period, productType: 'AUTO_RENEWABLE_SUBSCRIPTION', introPrice: week },
  });
  const offers = offersFromPackages([
    usd('com.swingguru.app.pro.monthly', 'MONTHLY', 9.99, 'P1M'),
    usd('com.swingguru.app.pro.yearly', 'ANNUAL', 59.99, 'P1Y'),
  ], { 'com.swingguru.app.pro.monthly': 2, 'com.swingguru.app.pro.yearly': 2 });
  const yearly = offers.find((o) => o.kind === 'yearly')!;
  assert.equal(trialHeadline(yearly), '7 days free');
  assert.equal(trialPriceLine(yearly), 'then $59.99/year');
  assert.equal(yearly.value, 'Save 49% vs monthly');          // 59.99 vs 119.88 = 49.96 % -> 49 %
  const view = paywallView(status(), offers, yearly, false);
  assert.equal(view.ctaLabel, 'Start free trial');
  assert.match(String(view.disclosure), /^Free for 7 days, then \$59\.99 per year\./);
  // Not eligible: plain card, no trial headline, Subscribe with the price.
  const plain = offersFromPackages([usd('com.swingguru.app.pro.yearly', 'ANNUAL', 59.99, 'P1Y')], { 'com.swingguru.app.pro.yearly': 1 })[0];
  assert.equal(trialHeadline(plain), null);
  assert.equal(ctaLabel(plain), 'Subscribe · $59.99');
  // The screen uses the trial card for yearly only.
  const { readFileSync } = await import('node:fs');
  const paywall = readFileSync(new URL('../src/app/paywall.tsx', import.meta.url), 'utf8');
  assert.match(paywall, /const headline = offer\.kind === 'yearly' \? trialHeadline\(offer\) : null;/);
  assert.match(paywall, /\{offer\.title\}, \{priceLine\}/);
});

test('post-settle copy distinguishes no-purchase, pending, unavailable and active', () => {
  assert.equal(afterSettleCopy('active', true, 'purchase'), null);

  const pending = afterSettleCopy('pending', true, 'restore');
  assert.equal(pending?.title, 'Almost there');
  assert.match(String(pending?.body), /verifying/);

  // Nothing was confirmed and the SDK has no entitlement: never claim the purchase exists.
  const unavailableEmpty = afterSettleCopy('unavailable', false, 'restore');
  assert.match(String(unavailableEmpty?.body), /couldn't check your plan/i);
  assert.ok(!/went through|has your purchase/i.test(String(unavailableEmpty?.body)));

  // A fresh SDK entitlement with an unreachable server is a verification problem, not a failure.
  const unavailableEntitled = afterSettleCopy('unavailable', true, 'purchase');
  assert.match(String(unavailableEntitled?.body), /verifying it now/);

  const noPurchases = afterSettleCopy('inactive', false, 'restore');
  assert.equal(noPurchases?.title, 'No purchases to restore');
  assert.match(String(noPurchases?.body), /signed in to the same Apple ID/);

  const notConfirmed = afterSettleCopy('inactive', false, 'purchase');
  assert.match(String(notConfirmed?.body), /isn't active on your account yet/);
  // Never claims Apple charged or did not charge: only what the server verified.
  assert.ok(!/charged/i.test(String(notConfirmed?.body)));

  const changed = afterSettleCopy('identity_changed', false, 'purchase');
  assert.equal(changed?.tone, 'error');
  assert.match(String(changed?.body), /sign in again/i);

  // A verified-but-anonymous free golfer is asked for an account, never nudged toward a purchase.
  const anonymous = afterSettleCopy('sign_in_required', false, 'purchase');
  assert.equal(anonymous?.title, 'Sign-in needed');
  assert.match(String(anonymous?.body), /account that stays with you/i);
  assert.ok(!/plan|purchase|buy|subscribe/i.test(String(anonymous?.body)));
});
