// Practice reminders (1.0.6): local only, 5 days after the latest own analysis, quiet hours respected,
// asked only after a tap on a result, and never needing the Push Notifications entitlement.
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { test } from 'node:test';

import {
  REMIND_AFTER_DAYS, reminderContent, reminderDate, reminderDayLabel, shouldOfferReminder, shouldSchedule,
} from '../src/lib/reminders-core.ts';

const read = (p: string) => readFileSync(new URL(`../${p}`, import.meta.url), 'utf8');

test('reminder lands 5 days later, moved to 6 pm when it would be early morning or late night', () => {
  assert.equal(REMIND_AFTER_DAYS, 5);
  const afternoon = reminderDate(new Date(2026, 10, 16, 14, 30));
  assert.deepEqual([afternoon.getDate(), afternoon.getHours(), afternoon.getMinutes()], [21, 14, 30]);
  const late = reminderDate(new Date(2026, 10, 16, 22, 15));
  assert.deepEqual([late.getDate(), late.getHours(), late.getMinutes()], [21, 18, 0]);
  const early = reminderDate(new Date(2026, 10, 16, 6, 0));
  assert.deepEqual([early.getDate(), early.getHours()], [21, 18]);
  const monthEnd = reminderDate(new Date(2026, 10, 28, 12, 0));
  assert.deepEqual([monthEnd.getMonth(), monthEnd.getDate()], [11, 3]);
});

test('1-minute test reminders: staging/TestFlight profiles only, ignored against production', async () => {
  const { reminderTestSeconds } = await import('../src/lib/reminders-core.ts');
  const staging = 'https://swing-guru-staging-production.up.railway.app';
  assert.equal(reminderTestSeconds('60', staging), 60);
  assert.equal(reminderTestSeconds('60', 'https://swing-guru-api-production.up.railway.app'), 0);   // never in production
  assert.equal(reminderTestSeconds(undefined, staging), 0);
  assert.equal(reminderTestSeconds('abc', staging), 0);
  const t = new Date(2026, 10, 16, 23, 0);
  assert.equal(reminderDate(t, 60).getTime() - t.getTime(), 60_000);   // test mode skips quiet hours
  const eas = JSON.parse(read('eas.json'));
  assert.equal(eas.build.production.env?.EXPO_PUBLIC_REMINDER_TEST_SECONDS, undefined);
  assert.equal(eas.build.testflight.env.EXPO_PUBLIC_REMINDER_TEST_SECONDS, '60');
  assert.equal(eas.build.staging.env.EXPO_PUBLIC_REMINDER_TEST_SECONDS, '60');
});

test('reminder copy names the fix and club, and never the score', () => {
  const c = reminderContent({ fix: 'Impact extension', club: 'Driver' });
  assert.match(c.body, /impact extension/);
  assert.match(c.body, /driver swing/);
  assert.doesNotMatch(c.title + c.body, /\d/);
  assert.match(reminderContent({ fix: null, club: null }).body, /^Film your swing/);
});

test('day label: today, tomorrow, then the weekday', () => {
  const now = new Date(2026, 10, 16, 10);
  assert.equal(reminderDayLabel(new Date(2026, 10, 16, 18), now), 'today');
  assert.equal(reminderDayLabel(new Date(2026, 10, 17, 9), now), 'tomorrow');
  assert.equal(reminderDayLabel(new Date(2026, 10, 21, 14), now), 'Saturday');
});

test('offered on a fresh own result before the golfer chooses; scheduled only when on and allowed', () => {
  const base = { fresh: true, example: false, permission: 'undetermined' as const, choice: null };
  assert.equal(shouldOfferReminder(base), true);
  assert.equal(shouldOfferReminder({ ...base, example: true }), false);
  assert.equal(shouldOfferReminder({ ...base, fresh: false }), false);
  assert.equal(shouldOfferReminder({ ...base, choice: 'off' }), false);
  assert.equal(shouldOfferReminder({ ...base, permission: 'denied' }), false);
  assert.equal(shouldSchedule({ ...base, choice: 'on', permission: 'granted' }), true);
  assert.equal(shouldSchedule({ ...base, choice: 'on', permission: 'denied' }), false);
  assert.equal(shouldSchedule({ ...base, choice: 'off', permission: 'granted' }), false);
  assert.equal(shouldSchedule({ ...base, example: true, choice: 'on', permission: 'granted' }), false);
});

test('local notifications only: no push token, no server, push entitlement stripped', () => {
  const lib = read('src/lib/reminders.ts');
  assert.doesNotMatch(lib, /getExpoPushTokenAsync|getDevicePushTokenAsync|fetch\(|api\./);
  assert.match(lib, /SchedulableTriggerInputTypes\.DATE/);
  assert.match(lib, /cancelReminder\(\);\s*\n\s*const date = reminderDate/);   // one pending reminder at a time
  const app = JSON.parse(read('app.json'));
  const names = app.expo.plugins.map((p: unknown) => (Array.isArray(p) ? p[0] : p));
  // The stripper must be listed BEFORE expo-notifications: Expo runs entitlement mods in reverse order.
  assert.ok(names.indexOf('./plugins/without-push-entitlement') >= 0);
  assert.ok(names.indexOf('./plugins/without-push-entitlement') < names.indexOf('expo-notifications'));
  assert.match(read('plugins/without-push-entitlement.js'), /delete cfg\.modResults\['aps-environment'\]/);
});

test('first-swing reminder: 2 days later at 6 pm, fixed copy, 1 minute in test builds', async () => {
  const { FIRST_SWING_AFTER_DAYS, FIRST_SWING_CONTENT, firstSwingReminderDate } = await import('../src/lib/reminders-core.ts');
  assert.equal(FIRST_SWING_AFTER_DAYS, 2);
  const at = firstSwingReminderDate(new Date(2026, 10, 17, 9, 40));
  assert.deepEqual([at.getDate(), at.getHours(), at.getMinutes()], [19, 18, 0]);
  const monthEnd = firstSwingReminderDate(new Date(2026, 10, 30, 22, 0));
  assert.deepEqual([monthEnd.getMonth(), monthEnd.getDate(), monthEnd.getHours()], [11, 2, 18]);
  const t = new Date(2026, 10, 17, 23, 0);
  assert.equal(firstSwingReminderDate(t, 60).getTime() - t.getTime(), 60_000);
  assert.equal(FIRST_SWING_CONTENT.body, 'At the range this week? Film one swing and get your score in a minute.');
});

test('first-swing offer: only with no own swings, unanswered, reminders not off, iOS still askable', async () => {
  const { shouldOfferFirstSwingReminder } = await import('../src/lib/reminders-core.ts');
  const base = { ownSwings: 0 as number | null, permission: 'undetermined' as const, choice: null, firstChoice: null };
  assert.equal(shouldOfferFirstSwingReminder(base), true);
  assert.equal(shouldOfferFirstSwingReminder({ ...base, ownSwings: 1 }), false);
  assert.equal(shouldOfferFirstSwingReminder({ ...base, ownSwings: null }), false);        // count not loaded yet
  assert.equal(shouldOfferFirstSwingReminder({ ...base, firstChoice: 'declined' }), false);
  assert.equal(shouldOfferFirstSwingReminder({ ...base, firstChoice: 'scheduled' }), false);
  assert.equal(shouldOfferFirstSwingReminder({ ...base, choice: 'off' }), false);
  assert.equal(shouldOfferFirstSwingReminder({ ...base, permission: 'denied' }), false);
});

test('first-swing reminder: asked from a tap only, on the example and empty Home; cancelled by a real result', () => {
  const lib = read('src/lib/reminders.ts');
  assert.match(lib, /export async function remindFirstSwing[\s\S]*?await enableReminders\(from\)/);
  assert.match(lib, /export async function cancelReminder\(\)[\s\S]*?await cancelFirstSwingReminder\(\);/);   // off / account change
  assert.match(lib, /kind !== KIND && kind !== FIRST_KIND/);                                                 // tap opens Analyse
  const card = read('src/components/first-swing-reminder.tsx');
  assert.match(card, /label="Remind me at my next practice"/);
  assert.match(card, /Not now/);
  assert.doesNotMatch(card, /useEffect\([^)]*remindFirstSwing/);
  assert.match(read('src/app/swing/[id].tsx'), /<ExampleFirstSwingReminder \/>/);
  const home = read('src/app/(tabs)/index.tsx');
  assert.match(home, /<FirstSwingReminderCard from="home" ownSwings=\{0\} \/>/);
  assert.match(home, /if \(count\) void cancelFirstSwingReminder\(\);/);
  assert.match(read('src/components/reminder-card.tsx'), /if \(fresh && !example\) void cancelFirstSwingReminder\(\);/);
});

test('permission is asked only from a tap (result card or Profile), never on launch', () => {
  const layout = read('src/app/_layout.tsx');
  assert.doesNotMatch(layout, /enableReminders|requestPermissionsAsync/);
  assert.match(layout, /useReminderTaps\(\)/);
  const card = read('src/components/reminder-card.tsx');
  assert.match(card, /label="Remind me"/);
  assert.match(card, /No thanks/);
  assert.match(read('src/app/swing/[id].tsx'), /<ReminderCard swingId=\{swing\.id\} fresh=\{Boolean\(fresh\)\} example=\{example\}/);
  assert.match(read('src/app/(tabs)/profile.tsx'), /<ReminderSetting \/>/);
  const analytics = read('src/lib/analytics.ts');
  for (const e of ['reminder_prompt_viewed', 'reminder_enabled', 'reminder_declined', 'reminder_disabled', 'reminder_scheduled', 'reminder_opened']) {
    assert.match(analytics, new RegExp(`'${e}'`), e);
  }
});
