// 1.0.7: the Pro trial offer as the last welcome screen, and no post-first-score offer after a trial started.
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { test } from 'node:test';

import { isFirstResult, onboardingPaywallSkipReason, shouldAutoOpenPaywall, trialAlreadyStarted } from '../src/lib/first-result.ts';

const read = (p: string) => readFileSync(new URL(`../${p}`, import.meta.url), 'utf8');

test('trial already started: recorded here, Pro active (running trial), or the App Store says the trial was used', () => {
  const base = { recordedHere: false, access: 'free', eligibilities: ['eligible', 'no_offer'] as const };
  assert.equal(trialAlreadyStarted({ ...base, eligibilities: [...base.eligibilities] }), false);
  assert.equal(trialAlreadyStarted({ ...base, eligibilities: [], recordedHere: true }), true);
  assert.equal(trialAlreadyStarted({ ...base, eligibilities: [], access: 'pro' }), true);
  assert.equal(trialAlreadyStarted({ ...base, eligibilities: ['ineligible', 'no_offer'] }), true);
  assert.equal(trialAlreadyStarted({ ...base, eligibilities: ['unknown'] }), false);     // unknown is not "used"
});

test('post-first-score paywall: still opens once for free golfers, never after a trial started', () => {
  const free = { enabled: true, access: 'free', seenBefore: false };
  assert.equal(shouldAutoOpenPaywall(free), true);
  assert.equal(shouldAutoOpenPaywall({ ...free, trialStarted: false }), true);
  assert.equal(shouldAutoOpenPaywall({ ...free, trialStarted: true }), false);
  assert.equal(shouldAutoOpenPaywall({ ...free, seenBefore: true }), false);
});

test('first result: just finished OR the only completed swing opened from anywhere (left Processing early)', () => {
  assert.equal(isFirstResult({ fresh: true, example: false, ownCompleted: null }), true);
  assert.equal(isFirstResult({ fresh: false, example: false, ownCompleted: 1 }), true);    // opened from Home/Library
  assert.equal(isFirstResult({ fresh: false, example: false, ownCompleted: 3 }), false);   // an older swing
  assert.equal(isFirstResult({ fresh: false, example: false, ownCompleted: null }), false); // list not loaded yet
  assert.equal(isFirstResult({ fresh: true, example: true, ownCompleted: 1 }), false);     // never the example
  assert.equal(isFirstResult({ fresh: false, example: true, ownCompleted: 1 }), false);
  const summary = read('src/app/swing/[id].tsx');
  assert.match(summary, /isFirstResult\(\{ fresh: Boolean\(fresh\), example, ownCompleted: ownSwings \? ownSwings\.length : null \}\)/);
  assert.match(summary, /\{firstResult \? <ProOfferCard /);
  assert.doesNotMatch(summary, /\{fresh && !example \? <ProOfferCard/);
});

test('onboarding offer: shown when the plan check fails or is slow; skipped only on a definite answer', () => {
  const onboarding = read('src/app/onboarding.tsx');
  assert.match(onboarding, /\}\)\(\)\.catch\(\(\) => null\);/);
  assert.match(onboarding, /new Promise<null>\(\(resolve\) => setTimeout\(\(\) => resolve\(null\), PRO_CHECK_WAIT_MS\)\)/);
  assert.match(onboarding, /if \(offer !== false && !offered\.current\) \{/);
});

test('1.0.8: the analytics question is on the first welcome screen (opt-in, Home keeps it for skippers)', () => {
  const onboarding = read('src/app/onboarding.tsx');
  assert.match(onboarding, /<AnalyticsConsentCard style=\{\{ marginTop: 20 \}\} \/>/);
  assert.match(read('src/app/(tabs)/index.tsx'), /<AnalyticsConsentCard \/>/);
  const card = read('src/components/analytics-consent.tsx');
  assert.match(card, /analyticsAvailable && answer === 'unset'/);
  assert.match(card, /setConsent\('granted'\)/);
  assert.match(card, /setConsent\('denied'\)/);
});

test('onboarding offer: everyone without Pro (server plan only, no App Store wait); skip reasons recorded', () => {
  const ok = { supported: true, enabled: true, access: 'free', trialStarted: false };
  assert.equal(onboardingPaywallSkipReason(ok), null);
  assert.equal(onboardingPaywallSkipReason({ ...ok, access: 'exhausted' }), null);
  assert.equal(onboardingPaywallSkipReason({ ...ok, supported: false }), 'billing_off');
  assert.equal(onboardingPaywallSkipReason({ ...ok, enabled: false }), 'server_billing_off');
  assert.equal(onboardingPaywallSkipReason({ ...ok, access: 'disabled' }), 'server_billing_off');
  assert.equal(onboardingPaywallSkipReason({ ...ok, access: 'pro' }), 'pro');
  assert.equal(onboardingPaywallSkipReason({ ...ok, trialStarted: true }), 'trial_started');
  const onboarding = read('src/app/onboarding.tsx');
  assert.doesNotMatch(onboarding, /loadOffers/);                                     // no App Store lookup before the screen
  assert.match(onboarding, /track\('onboarding_paywall_skipped', \{ reason:/);
});

test('example summary: Analyse your own swing and Done are pinned to the bottom, always visible', () => {
  const summary = read('src/app/swing/[id].tsx');
  assert.match(summary, /\{example \? \(\s*<View style=\{styles\.exampleBar\}>\s*<Button label="Analyse your own swing"/);
  assert.match(summary, /edges=\{example \? \['top', 'bottom'\] : \['top'\]\}/);
});

test('onboarding shows the offer before the last screen; the paywall has the copy, Not now and the events', () => {
  const onboarding = read('src/app/onboarding.tsx');
  assert.match(onboarding, /STEPS\[to\] === 'example' && billingSupported/);
  // Continue waits for the background trial check (up to 5 s) instead of skipping the offer.
  assert.match(onboarding, /const PRO_CHECK_WAIT_MS = 5000;/);
  assert.match(onboarding, /await Promise\.race\(\[\s*proCheck\.current,/);
  assert.match(onboarding, /<Button label="Continue" block busy=\{checking\}/);
  assert.match(onboarding, /router\.push\(\{ pathname: '\/paywall', params: \{ from: 'onboarding' \} \}\)/);
  const paywall = read('src/app/paywall.tsx');
  assert.match(paywall, /Fix your swing faster with Pro/);
  for (const b of ['Unlimited swing analyses', 'Personal drills for every fault', 'Progress tracking and leagues']) {
    assert.match(paywall, new RegExp(b), b);
  }
  assert.match(paywall, /result\.offers\.find\(\(o\) => o\.kind === 'yearly'\)/);         // yearly pre-selected
  assert.match(paywall, /<TrialTimeline offer=\{selected\} \/>/);                          // Today / Day 7
  assert.match(paywall, /label=\{onboarding \? 'Not now, use my free analysis' : 'Not now'\}/);
  assert.match(paywall, /track\('onboarding_paywall_shown'/);
  assert.match(paywall, /track\('onboarding_paywall_dismissed'/);
  assert.match(paywall, /track\('onboarding_paywall_trial_started'/);
  assert.match(paywall, /markTrialStarted\(\)/);
  assert.match(read('src/components/pro-offer.tsx'), /shouldAutoOpenPaywall\(\{[^}]*trialStarted \}\)/);
  const analytics = read('src/lib/analytics.ts');
  for (const e of ['onboarding_paywall_shown', 'onboarding_paywall_dismissed', 'onboarding_paywall_trial_started']) {
    assert.match(analytics, new RegExp(`'${e}'`), e);
  }
});

test('trial timeline still shows today, the last free day and when payment starts', async () => {
  const { trialTimeline } = await import('../src/lib/billing-core.ts');
  const steps = trialTimeline({ trial: { days: 7 }, period: 'year', priceString: '£39.99' } as never)!;
  assert.deepEqual(steps.map((s) => s.when), ['Today', 'Before day 7', 'Day 7']);
  assert.match(steps[2].what, /£39\.99 per year starts/);
});
