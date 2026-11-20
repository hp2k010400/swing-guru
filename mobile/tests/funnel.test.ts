// New-user funnel (1.0.5): onboarding once for new golfers, a read-only free example, filming tips after
// filming-related failures, and the events that measure each step.
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { test } from 'node:test';

import { FILMING_TIPS, filmingTip } from '../src/lib/filming-tips.ts';
import { shouldShowOnboarding } from '../src/lib/onboarding.ts';

const read = (p: string) => readFileSync(new URL(`../${p}`, import.meta.url), 'utf8');

test('onboarding: only new golfers with nothing uploading, and only once', () => {
  assert.equal(shouldShowOnboarding(0, 0, false), true);
  assert.equal(shouldShowOnboarding(3, 0, false), false);   // existing users never see it
  assert.equal(shouldShowOnboarding(0, 1, false), false);   // a swing is already uploading
  assert.equal(shouldShowOnboarding(0, 0, true), false);    // seen or skipped
  assert.equal(shouldShowOnboarding(null, 0, false), false); // swings not loaded yet
});

test('every filming failure has a friendly tip; real faults do not', () => {
  for (const code of ['no_setup', 'no_swing', 'framing', 'camera_angle', 'too_far']) {
    const tip = filmingTip(code);
    assert.ok(tip, code);
    assert.doesNotMatch(tip!.body, /error|invalid|failed|detect/i, code);   // plain language
  }
  assert.equal(filmingTip('failed'), null);
  assert.equal(filmingTip('timeout'), null);
  assert.equal(filmingTip(undefined), null);
  assert.equal(Object.keys(FILMING_TIPS).length, 5);
});

test('the example is read-only, labelled, and never billed, rated or saved', () => {
  const summary = read('src/app/swing/[id].tsx');
  assert.match(summary, /id === EXAMPLE_ID \? api\.example\(\) : api\.swing\(id\)/);
  assert.match(summary, />EXAMPLE</);
  assert.match(summary, /doesn't use your free analysis/);
  // Edit, delete, compare and feedback live only in the non-example branch.
  const flat = summary.replace(/\r\n/g, '\n');
  const start = flat.indexOf('Analyse your own swing');
  const exampleBranch = flat.slice(flat.lastIndexOf('{example ? (', start), flat.indexOf(') : (', start));
  assert.match(exampleBranch, /Analyse your own swing/);
  assert.doesNotMatch(exampleBranch, /deleteSwing|Delete swing|compare|AnalysisFeedback/);
  // Rating needs swing.id === id, which the example route ('example') never matches.
  assert.match(summary, /swing\.id !== id/);
  // No Pro offer, first-result paywall or save prompt on the example.
  // (isFirstResult is always false for the example: tests/onboarding-paywall.test.ts.)
  assert.match(summary, /\{firstResult \? <ProOfferCard fix=\{biggestFix\(swing\.score\)\} \/> : null\}/);
  assert.match(summary, /<SaveSwingCard fresh=\{Boolean\(fresh\)\} example=\{example\} \/>/);
  const api = read('src/lib/api.ts');
  assert.match(api, /example: \(\) => request<SwingDetail>\('GET', '\/v1\/example'\)/);
});

test('failure screens send golfers straight back to record or choose', () => {
  const processing = read('src/app/processing/[id].tsx');
  assert.match(processing, /const tip = filmingTip\(job\.error\.code\)/);
  assert.match(processing, /track\('retry_after_failure', \{ error_code: code \}\)/);
  assert.match(processing, /router\.replace\('\/analyze'\)/);
  assert.match(processing, /trackOnce\('swing_analysis_failed', id, \{ error_code:/);  // analysis_failed with its code
});

test('See the example opens straight into the replay, with the Summary underneath', () => {
  const example = read('src/lib/example.ts');
  assert.match(example, /router\.push\(\{ pathname: '\/replay\/\[id\]', params: \{ id: EXAMPLE_ID \} \}\)/);
  assert.match(read('src/app/onboarding.tsx'), /openExample\(\{ replace: true \}\)/);
  assert.match(read('src/app/(tabs)/index.tsx'), /openExample\(\)/);
});

test('the filming picture is a right-handed golfer (mirrored, ball to the right)', () => {
  const art = read('src/components/filming-illustration.tsx');
  assert.match(art, /scale\(\$\{-scale\} \$\{scale\}\)/);
  assert.match(art, /const ballX = golferX \+ 48 \* scale/);
});

test('funnel events are declared and fired', () => {
  const analytics = read('src/lib/analytics.ts');
  for (const e of ['onboarding_viewed', 'onboarding_skipped', 'onboarding_completed', 'example_opened', 'example_cta_tapped', 'retry_after_failure']) {
    assert.match(analytics, new RegExp(`'${e}'`), e);
  }
  const onboarding = read('src/app/onboarding.tsx');
  // example_cta_tapped belongs to "Analyse your own swing" on the example; opening is example_opened.
  assert.match(read('src/app/swing/[id].tsx'), /track\('example_cta_tapped'/);
  assert.match(read('src/app/swing/[id].tsx'), /track\('example_opened'/);
  for (const e of ['onboarding_viewed', 'onboarding_skipped', 'onboarding_completed']) {
    assert.match(onboarding, new RegExp(`track\\('${e}'`), e);
  }
});

test('first result: the trial paywall opens once, never for Pro; save is offered to guests only', async () => {
  const { shouldAutoOpenPaywall, shouldOfferSave } = await import('../src/lib/first-result.ts');
  assert.equal(shouldAutoOpenPaywall({ enabled: true, access: 'free', seenBefore: false }), true);
  assert.equal(shouldAutoOpenPaywall({ enabled: true, access: 'free', seenBefore: true }), false);
  assert.equal(shouldAutoOpenPaywall({ enabled: true, access: 'pro', seenBefore: false }), false);
  assert.equal(shouldAutoOpenPaywall({ enabled: false, access: 'free', seenBefore: false }), false);
  assert.equal(shouldOfferSave({ fresh: true, anonymous: true, appleAvailable: true, example: false }), true);
  assert.equal(shouldOfferSave({ fresh: true, anonymous: false, appleAvailable: true, example: false }), false);
  assert.equal(shouldOfferSave({ fresh: false, anonymous: true, appleAvailable: true, example: false }), false);
  assert.equal(shouldOfferSave({ fresh: true, anonymous: true, appleAvailable: true, example: true }), false);
  const offer = read('src/components/pro-offer.tsx');
  assert.match(offer, /setTimeout\(\(\) => alive && open\(\), Math\.max\(0, DELAY_MS - \(Date\.now\(\) - startedAt\)\)\)/);   // after the score reveal, cancelled on leave
});

test('paywall: honest trial timeline, yearly first, Not now after a first result', async () => {
  const { trialTimeline } = await import('../src/lib/billing-core.ts');
  const yearly = { id: 'y', kind: 'yearly', title: 'Yearly', priceString: '£49.99', price: 49.99, currencyCode: 'GBP',
    value: null, period: 'year', trial: { days: 7, priceString: '£0.00' }, renewalCopy: null } as never;
  const steps = trialTimeline(yearly)!;
  assert.deepEqual(steps.map((s) => s.when), ['Today', 'Before day 7', 'Day 7']);
  assert.match(steps[2].what, /£49\.99 per year/);
  assert.doesNotMatch(steps.map((s) => s.what).join(' '), /remind/i);              // no reminder we don't send
  assert.equal(trialTimeline({ ...(yearly as object), trial: null } as never), null);
  const paywall = read('src/app/paywall.tsx');
  assert.match(paywall, /o\.kind === 'yearly'/);                                      // yearly selected by default
  assert.match(paywall, /source === 'first_result' \|\| onboarding \? \(\s*<Button label=\{onboarding \? 'Not now, use my free analysis' : 'Not now'\}/);
});

test('guided camera: records before the set-up, stops itself, asks for the camera only behind one Continue', async () => {
  const { recordingPrompt, SETUP_SECONDS, MAX_SECONDS } = await import('../src/lib/recording.ts');
  assert.match(recordingPrompt(false, 0).title, /Line up/);
  assert.match(recordingPrompt(true, 0).title, /set up now/);
  assert.match(recordingPrompt(true, SETUP_SECONDS).title, /Swing/);
  assert.ok(MAX_SECONDS >= 15 && MAX_SECONDS <= 30);
  const record = read('src/app/record.tsx');
  assert.match(record, /recordAsync\(\{ maxDuration: MAX_SECONDS \}\)/);
  assert.match(record, /videoQuality="1080p"/);                     // never 4K (the server failures)
  assert.match(record, /\bmute\b/);                                  // no microphone prompt
  // Apple: the pre-permission screen has a single Continue, no Allow and no way to cancel past it.
  const primer = record.slice(record.indexOf('if (!permission.granted)'), record.indexOf('async function start'));
  assert.match(primer, /label="Continue"/);
  assert.doesNotMatch(primer, /label="Allow"|label="Not now"|label="Cancel"/);
  assert.match(primer, /draft|Choose a video instead/);                // when blocked, a way forward without the camera
  assert.match(record, /draft\.start\(video\)/);
  assert.match(read('src/app/analyze.tsx'), /router\.push\('\/record'\)/);
  assert.match(read('src/app/_layout.tsx'), /name="record"/);
});

test('not at the range: onboarding offers the example, filming now, or uploading', () => {
  const onboarding = read('src/app/onboarding.tsx');
  assert.match(onboarding, /router\.replace\('\/record'\)/);
  assert.match(onboarding, /Not at the range\? Upload a video/);
  assert.match(onboarding, /Your first analysis is free/);
});
