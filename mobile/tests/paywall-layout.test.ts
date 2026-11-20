// Build 8 QA (2026-10-05): the Plans close button sat under the status bar. The screen is a
// fullScreenModal; a SafeAreaView measured inside it can read a zero top inset while the modal
// slides up. The header must sit below the device's real top inset, with a 44 pt target.
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { test } from 'node:test';

const source = readFileSync(new URL('../src/app/paywall.tsx', import.meta.url), 'utf8');

test('the Plans screen pads by the real safe-area insets, not a measured SafeAreaView or a fixed top', () => {
  assert.match(source, /const insets = useSafeAreaInsets\(\);/);
  assert.match(source, /paddingTop: insets\.top, paddingBottom: insets\.bottom/);
  assert.doesNotMatch(source, /<SafeAreaView/);
  assert.doesNotMatch(source, /\btop:\s*\d/);   // no hardcoded top offset anywhere on the screen
});

test('the close button is a 44 x 44 pt target with extra hit slop', () => {
  assert.match(source, /round: \{ width: 44, height: 44,/);
  assert.match(source, /accessibilityLabel=\{onboarding \? 'Not now' : 'Close'\} onPress=\{dismiss\} hitSlop=\{8\}/);
  assert.match(source, /const dismiss = \(\) => \{[\s\S]*?router\.back\(\);\s*\};/);
});

test('on iPhone-sized layouts the whole close target sits below the unsafe area', () => {
  const headerPad = Number(/header: \{[^}]*paddingVertical: (\d+)/.exec(source)?.[1]);
  const size = Number(/round: \{ width: (\d+), height: (\d+)/.exec(source)?.[2]);
  const slop = Number(/hitSlop=\{(\d+)\}/.exec(source)?.[1]);
  // Real portrait top insets (pt): SE 20, 13 mini 50, 14/15 59, 16 Pro 62, 16 Pro Max 62.
  for (const top of [20, 47, 50, 59, 62]) {
    const buttonTop = top + headerPad;
    assert.ok(buttonTop - slop >= top, `inset ${top}: the hit area must start below the unsafe area`);
    assert.ok(size >= 44, 'Apple minimum touch target');
  }
});
