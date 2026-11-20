// Build 9 release batch: free-limit copy, feedback payloads, safe-area audit, live links.
import assert from 'node:assert/strict';
import { readdirSync, readFileSync, statSync } from 'node:fs';
import { join } from 'node:path';
import { test } from 'node:test';
import { fileURLToPath } from 'node:url';

import type { PlanOffer } from '../src/lib/billing-core.ts';
import { analysisFeedback, generalFeedback, versionLabel } from '../src/lib/feedback-core.ts';
import { limitBanner } from '../src/lib/paywall-view.ts';
import type { BillingStatus } from '../src/lib/types.ts';

const SRC = fileURLToPath(new URL('../src/', import.meta.url));

function status(overrides: Partial<BillingStatus> = {}): BillingStatus {
  return {
    api_version: 'v1', enabled: true, access: 'exhausted', free_total: 3, free_used: 3, free_reserved: 0,
    free_remaining: 0, can_analyse: false, entitlement_active: false, product_id: null, expires_at: null,
    management_url: null, paid_allowance: 'unlimited', requires_sign_in: false,
    allowance_period_start: '2026-10-01T00:00:00+00:00', allowance_resets_at: '2026-11-01T00:00:00+00:00',
    allowance_timezone: 'UTC', allowance_model: 'calendar_month', ...overrides,
  };
}
const offer = (kind: PlanOffer['kind'], trial: boolean): PlanOffer => ({
  id: kind, kind, title: kind, priceString: '£1', price: 1, currencyCode: 'GBP', value: null,
  period: kind === 'lifetime' ? null : kind === 'yearly' ? 'year' : 'month',
  trial: trial ? { days: 14, priceString: '£0.00' } : null, renewalCopy: null, badge: null,
  eligibility: trial ? 'eligible' : 'ineligible',
});

// ---------------------------------------------------------------- free limit -> paywall

test('the limit banner explains the used allowance, the reset date and Pro', () => {
  const banner = limitBanner(status(), [offer('yearly', true), offer('lifetime', false)]);
  assert.equal(banner?.title, "You've used your 3 free analyses this month.");
  assert.match(String(banner?.body), /^Your free analyses reset on .+\. Upgrade to Swing Guru Pro to keep analysing now\.$/);
  assert.equal(banner?.trial, 'Start with a 14-day free trial.');
});

test('no trial claim unless StoreKit confirmed one, and never from lifetime', () => {
  assert.equal(limitBanner(status(), [offer('yearly', false), offer('monthly', false)])?.trial, null);
  // A (mis)configured lifetime trial must never produce the claim.
  const lifetimeOnly = limitBanner(status(), [{ ...offer('lifetime', false), trial: { days: 14, priceString: '£0' } }]);
  assert.equal(lifetimeOnly?.trial, null);
});

test('the banner only appears when the server says the allowance is used up', () => {
  for (const access of ['free', 'pro', 'sign_in_required', 'in_progress'] as const) {
    assert.equal(limitBanner(status({ access }), []), null, access);
  }
  assert.equal(limitBanner(status({ enabled: false }), []), null);
  assert.equal(limitBanner(null, []), null);
  assert.match(String(limitBanner(status({ allowance_resets_at: null }), [])?.body), /reset next month/);
});

// ---------------------------------------------------------------- feedback payloads

test('feedback payloads carry only what is needed', () => {
  const down = analysisFeedback('s1', 'down', '  head looked wrong ', ['impact.head'], '0.1.0 (9)');
  assert.deepEqual(down, { kind: 'analysis', swing_id: 's1', rating: 'down', message: 'head looked wrong',
                           measured_checks: ['impact.head'], app_version: '0.1.0 (9)' });
  // A thumbs up never carries a stale comment.
  assert.equal('message' in analysisFeedback('s1', 'up', 'old text', [], null), false);
  assert.equal(generalFeedback('bug', '   ', null), null);
  assert.equal(generalFeedback('feature', 'x'.repeat(2500), null)?.message.length, 2000);
  assert.equal(versionLabel('0.1.0', '9'), '0.1.0 (9)');
  assert.equal(versionLabel(null, '9'), null);
});

// ---------------------------------------------------------------- safe areas

function files(dir: string): string[] {
  return readdirSync(dir).flatMap((name) => {
    const path = join(dir, name);
    return statSync(path).isDirectory() ? files(path) : /\.tsx$/.test(name) ? [path] : [];
  });
}

test('no screen measures its own safe area: every container uses the real device insets', () => {
  for (const file of files(SRC)) {
    const source = readFileSync(file, 'utf8');
    assert.doesNotMatch(source, /<SafeAreaView/, file);
  }
});

test('every close / back control is at least 44 pt or carries hit slop', () => {
  for (const file of files(SRC)) {
    const source = readFileSync(file, 'utf8');
    // Each labelled control's own JSX (up to its closing '}>' of the style prop) must carry hitSlop.
    for (const match of source.matchAll(/accessibilityLabel="(Close|Back|Cancel|Close replay)"/g)) {
      const start = source.lastIndexOf('<Pressable', match.index);
      const tag = source.slice(start, source.indexOf('</Pressable>', match.index));
      assert.match(tag, /hitSlop=\{\d+\}/, `${file}: ${tag.slice(0, 90)}`);
    }
  }
  const replay = readFileSync(join(SRC, 'app/replay/[id].tsx'), 'utf8');
  assert.match(replay, /style=\{\[styles\.close, \{ top: insets\.top \+ 8 \}\]\}/);   // absolute: adds the inset itself
});

// ---------------------------------------------------------------- links

test('privacy, support and terms open the exact live site URLs', () => {
  const config = readFileSync(join(SRC, 'config.ts'), 'utf8');
  assert.match(config, /SITE_URL = 'https:\/\/swingguru-app\.netlify\.app'/);
  assert.match(config, /PRIVACY_URL = .*`\$\{SITE_URL\}\/privacy`/);
  assert.match(config, /SUPPORT_URL = .*`\$\{SITE_URL\}\/support`/);
  assert.match(config, /TERMS_URL = .*`\$\{SITE_URL\}\/terms`/);
  const profile = readFileSync(join(SRC, 'app/(tabs)/profile.tsx'), 'utf8');
  for (const url of ['PRIVACY_URL', 'SUPPORT_URL', 'TERMS_URL']) assert.ok(profile.includes(`openBrowserAsync(${url})`), url);
  const paywall = readFileSync(join(SRC, 'app/paywall.tsx'), 'utf8');
  for (const url of ['PRIVACY_URL', 'TERMS_URL']) assert.ok(paywall.includes(`openBrowserAsync(${url})`), url);
  assert.doesNotMatch(config, /https:\/\/swingguru\.app\//);   // not registered: would not resolve
});
