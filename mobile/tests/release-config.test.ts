// The production build must never carry staging configuration (2026-10-07 release candidate).
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { test } from 'node:test';

const eas = JSON.parse(readFileSync(new URL('../eas.json', import.meta.url), 'utf8'));

test('the production profile requests overlay v3 (Build 14) and nothing from staging', () => {
  const prod = eas.build.production;
  assert.equal(prod.environment, 'production');
  assert.deepEqual(prod.env, { EXPO_PUBLIC_OVERLAY_VERSION: '3' });
  const text = JSON.stringify(prod);
  for (const banned of ['staging', 'STAGING', 'APP_VARIANT', 'EXPO_PUBLIC_BILLING', 'EXPO_PUBLIC_AUTH_MODE', 'EXPO_PUBLIC_API_URL']) {
    assert.ok(!text.includes(banned), banned);
  }
});

test('the staging profile stays a separate app on the staging server', () => {
  const staging = eas.build.staging;
  assert.equal(staging.env.APP_VARIANT, 'staging');
  assert.match(staging.env.EXPO_PUBLIC_API_URL, /staging/);
  assert.equal(staging.env.EXPO_PUBLIC_BILLING, 'off');
  assert.equal(staging.env.EXPO_PUBLIC_OVERLAY_VERSION, '3');
});

test('the default app config is the release app; only APP_VARIANT=staging changes it', async () => {
  const configure = (await import('../app.config.js')).default as (c: { config: Record<string, unknown> }) => Record<string, unknown>;
  const base = JSON.parse(readFileSync(new URL('../app.json', import.meta.url), 'utf8')).expo;
  const previous = process.env.APP_VARIANT;
  delete process.env.APP_VARIANT;
  const release = configure({ config: base });
  assert.equal(release, base);                                     // untouched
  assert.equal((release.ios as { bundleIdentifier: string }).bundleIdentifier, 'com.swingguru.app');
  process.env.APP_VARIANT = 'staging';
  assert.equal((configure({ config: base }).ios as { bundleIdentifier: string }).bundleIdentifier, 'com.swingguru.app.staging');
  if (previous === undefined) delete process.env.APP_VARIANT; else process.env.APP_VARIANT = previous;
});


test('the testflight profile is the release app on the staging server, with billing off', () => {
  const tf = eas.build.testflight;
  // The same app (no APP_VARIANT: bundle id com.swingguru.app, App Store Connect app 6818093492)...
  assert.ok(!('APP_VARIANT' in tf.env));
  assert.equal(eas.submit.testflight.ios.ascAppId, eas.submit.production.ios.ascAppId);
  // ...that can only reach staging: never the production API, never purchases, dev sign-in only.
  assert.equal(tf.env.EXPO_PUBLIC_API_URL, 'https://swing-guru-staging-production.up.railway.app');
  assert.equal(tf.env.EXPO_PUBLIC_BILLING, 'off');
  assert.equal(tf.env.EXPO_PUBLIC_AUTH_MODE, 'dev');
  assert.equal(tf.environment, 'preview');                   // carries the staging access key
  assert.equal(tf.autoIncrement, true);                      // shares the build-number counter with production
});

test('1.0.2: Meta SDK is attribution-only - no IDFA, no ATT prompt, Meta SKAdNetwork ids', () => {
  const expo = JSON.parse(readFileSync(new URL('../app.json', import.meta.url), 'utf8')).expo;
  const fb = expo.plugins.find((p: unknown) => Array.isArray(p) && p[0] === 'react-native-fbsdk-next')?.[1];
  assert.ok(fb, 'Meta SDK plugin configured');
  assert.equal(fb.appID, '2096943041695834');
  assert.equal(fb.advertiserIDCollectionEnabled, false);     // never collect the IDFA
  assert.equal(fb.iosUserTrackingPermission, false);         // no ATT prompt / usage string
  assert.equal(fb.autoLogAppEventsEnabled, true);            // installs + launches for attribution
  assert.equal(expo.ios.infoPlist.NSUserTrackingUsageDescription, undefined);
});

test('1.0.8: the binary version is 1.0.8 and the bundle id is unchanged', () => {
  const expo = JSON.parse(readFileSync(new URL('../app.json', import.meta.url), 'utf8')).expo;
  assert.equal(expo.version, '1.0.8');                       // next App Store version after 1.0.7
  assert.equal(expo.ios.bundleIdentifier, 'com.swingguru.app');
  assert.equal(eas.cli.appVersionSource, 'remote');          // build number from EAS (13 -> 14)
  assert.equal(eas.build.production.autoIncrement, true);
  assert.deepEqual(eas.build.production.env, { EXPO_PUBLIC_OVERLAY_VERSION: '3' });   // Overlay V3 included
});

test('1.0.4: clips larger than 1080p are re-encoded to 1080p before upload, whatever their size', () => {
  // 12-13 Nov 2026: 9 of 10 4K clips (often ~12 MB, under the size threshold) failed on the server.
  const trim = readFileSync(new URL('../src/lib/trim.ts', import.meta.url), 'utf8');
  const analysis = readFileSync(new URL('../src/lib/analysis.ts', import.meta.url), 'utf8');
  assert.match(trim, /export const FIT_MAX_LONG_SIDE = 1920;/);
  assert.match(trim, /sizeBytes > FIT_THRESHOLD_BYTES \|\| \(longSide \?\? 0\) > FIT_MAX_LONG_SIDE/);
  assert.match(trim, /if \(needsFit\(out\.size, video\.longSide\)\)/);
  assert.match(analysis, /Math\.max\(asset\.width \?\? 0, asset\.height \?\? 0\)/);
});
