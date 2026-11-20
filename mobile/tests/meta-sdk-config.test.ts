import assert from 'node:assert/strict';
import { readdirSync, readFileSync, statSync } from 'node:fs';
import { join } from 'node:path';
import test from 'node:test';
import { fileURLToPath } from 'node:url';

import { analysesCount } from '../src/lib/billing-core';

// The Meta SDK starts natively through react-native-fbsdk-next's own Expo AppDelegate subscriber
// (ExpoAdapterFBSDKNext/FacebookAppDelegate.swift), plus Settings.initializeSDK() from JS.
test('the Meta SDK is configured by its own plugin only (no duplicate AppDelegate injection)', () => {
  const expo = JSON.parse(readFileSync(new URL('../app.json', import.meta.url), 'utf8')).expo;
  const names = expo.plugins.map((p: unknown) => (Array.isArray(p) ? p[0] : p));
  assert.ok(names.includes('react-native-fbsdk-next'));
  assert.ok(!names.some((n: string) => n.includes('with-facebook-app-delegate')));
});

function sources(dir: string): string[] {
  return readdirSync(dir).flatMap((name) => {
    const path = join(dir, name);
    return statSync(path).isDirectory() ? sources(path) : /\.(ts|tsx)$/.test(name) ? [path] : [];
  });
}

test('no device identifiers go to RevenueCat and no purchase is logged to Meta by hand', () => {
  const src = sources(fileURLToPath(new URL('../src', import.meta.url)))
    .map((f) => readFileSync(f, 'utf8')).join('\n');
  for (const banned of ['setFBAnonymousID', 'collectDeviceIdentifiers', 'setAdjustID', 'setAppsflyerID', 'logPurchase', 'fb_mobile_purchase']) {
    assert.ok(!src.includes(banned), `${banned} must not be used`);
  }
});

test('free allowance copy reads correctly for 1 or many', () => {
  assert.equal(analysesCount(1), '1 analysis');
  assert.equal(analysesCount(3), '3 analyses');
});
