// Phase 5 audit (2026-10-05): the Profile screen's Apple touchpoints. The flow logic is covered by
// identity-flow tests; these pin the screen wiring the audit fixed.
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { test } from 'node:test';

const profile = readFileSync(new URL('../src/app/(tabs)/profile.tsx', import.meta.url), 'utf8');
const block = (start: string, end: string) => profile.slice(profile.indexOf(start), profile.indexOf(end, profile.indexOf(start)));

test('repeated taps cannot open two Apple sheets', () => {
  const signIn = block('const appleSignIn = async', 'const signOut =');
  assert.match(signIn, /if \(appleLock\.current\) return;\s*appleLock\.current = true;/);
});

test('the switch question holds the lock, and both answers release it', () => {
  const signIn = block('const appleSignIn = async', 'const signOut =');
  assert.match(signIn, /handedOff = true/);
  assert.match(signIn, /text: 'Cancel', style: 'cancel', onPress: \(\) => \{ appleLock\.current = false; setSigningIn\(false\); \}/);
  assert.match(signIn, /cancelable: false/);   // Android back cannot dismiss without releasing
  const switchPart = signIn.slice(signIn.indexOf("text: 'Switch account'"));
  assert.match(switchPart, /catch \(error\)[\s\S]*finally \{\s*appleLock\.current = false;/);
});

test('sign out goes through the serialised, owner-bound teardown', () => {
  const signOut = block('const signOut =', '/**');
  assert.match(signOut, /teardown\.run\(account\.userId\)/);
  assert.doesNotMatch(signOut, /signOutLocally\(\)/);
});
