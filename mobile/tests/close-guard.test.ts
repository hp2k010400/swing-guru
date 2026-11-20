// Phase 3 regression: the Swing Analysis close. A repeated close must navigate once (no second
// router.back() popping the screen underneath), and the close must keep working if the first
// attempt did not leave the screen. Plus the screen wiring and the viewer's tap layers.
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { test } from 'node:test';

import { createCloseGuard } from '../src/lib/close-guard.ts';

const read = (path: string) => readFileSync(new URL(path, import.meta.url), 'utf8');

test('a double tap or a native X plus viewer close navigates exactly once', () => {
  let clock = 0;
  let navigations = 0;
  const close = createCloseGuard(() => { navigations += 1; }, () => clock);
  assert.equal(close(), true);
  clock = 40; assert.equal(close(), false);
  clock = 300; assert.equal(close(), false);
  assert.equal(navigations, 1);
});

test('the close responds again if the first navigation did not leave the screen', () => {
  let clock = 0;
  let navigations = 0;
  const close = createCloseGuard(() => { navigations += 1; }, () => clock);
  close();
  clock = 1300;
  assert.equal(close(), true);
  assert.equal(navigations, 2);
});

test('replay and summary closes go through the guard with an enlarged hit area', () => {
  for (const screen of ['../src/app/replay/[id].tsx', '../src/app/swing/[id].tsx']) {
    const source = read(screen);
    assert.match(source, /createCloseGuard\(/, screen);
    assert.match(source, /hitSlop=\{8\}/, screen);   // 40 pt drawn + 8 each side = 56 pt
    assert.doesNotMatch(source, /onPress=\{\(\) => \(router\.canGoBack\(\)/, screen);
  }
});

test('the viewer never lets its loading or error layer swallow the close', () => {
  const css = read('../../src/swingguru/viewer/app.css');
  const js = read('../../src/swingguru/viewer/app.js');
  assert.match(css, /\.loading \{ pointer-events: none; \}/);
  assert.match(css, /\.hud-top \{ z-index: 5; \}/);              // above .loading (4)
  assert.match(css, /\.hud-top \.round::after \{ content: ""; position: absolute; inset: -6px;/); // 32 + 12 = 44 px
  // Close is posted before anything that could throw, and only once per tap burst.
  const handler = js.slice(js.indexOf('var closing = false;'), js.indexOf('} else {', js.indexOf('var closing = false;')));
  assert.ok(handler.indexOf('notifyApp("close")') < handler.indexOf('pause()'));
  assert.match(handler, /if \(closing\) return;/);
});
