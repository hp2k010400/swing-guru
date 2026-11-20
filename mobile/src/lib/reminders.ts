// Practice reminders: LOCAL notifications only (scheduled on the phone, no server, no push token).
// iOS permission is asked only when the golfer taps "Remind me" on a result, never on launch.
// At most one reminder is pending: each new analysis replaces it, so it always counts from the latest swing.
import * as Notifications from 'expo-notifications';
import { router } from 'expo-router';
import { useEffect, useRef } from 'react';

import { API_URL } from '@/config';

import { track } from './analytics';
import {
  FIRST_SWING_CONTENT, firstSwingReminderDate, reminderContent, reminderDate, reminderTestSeconds, type ReminderPermission,
} from './reminders-core';

// Staging/TestFlight test builds set EXPO_PUBLIC_REMINDER_TEST_SECONDS=60 (eas.json); production never does.
const TEST_SECONDS = reminderTestSeconds(process.env.EXPO_PUBLIC_REMINDER_TEST_SECONDS, API_URL);

const CHOICE_KEY = 'sg.reminders.choice';   // 'on' | 'off' (absent: not asked yet)
const PENDING_KEY = 'sg.reminders.pending'; // id of the scheduled notification
const KIND = 'practice_reminder';
// First-swing reminder (no own analysis yet): its own pending id, and whether the offer was answered.
const FIRST_PENDING_KEY = 'sg.reminders.first.pending';
const FIRST_CHOICE_KEY = 'sg.reminders.first.choice'; // 'scheduled' | 'declined' (absent: not offered/answered)
const FIRST_KIND = 'first_swing_reminder';

// Show the reminder as a banner even if the app happens to be open when it fires.
Notifications.setNotificationHandler({
  handleNotification: async () => ({ shouldShowBanner: true, shouldShowList: true, shouldPlaySound: false, shouldSetBadge: false }),
});

function read(key: string): string | null {
  try {
    return localStorage.getItem(key);
  } catch {
    return null;
  }
}
function write(key: string, value: string | null): void {
  try {
    if (value === null) localStorage.removeItem(key);
    else localStorage.setItem(key, value);
  } catch {
    /* a convenience only */
  }
}

export function reminderChoice(): 'on' | 'off' | null {
  const v = read(CHOICE_KEY);
  return v === 'on' || v === 'off' ? v : null;
}

export async function reminderPermission(): Promise<ReminderPermission> {
  try {
    const p = await Notifications.getPermissionsAsync();
    if (p.granted) return 'granted';
    return p.canAskAgain ? 'undetermined' : 'denied';
  } catch {
    return 'denied';
  }
}

/** Asks iOS (first time only) and records the golfer's choice. Returns whether reminders are now on. */
export async function enableReminders(from: 'result' | 'profile' | 'example' | 'home'): Promise<boolean> {
  let granted = false;
  try {
    const p = await Notifications.requestPermissionsAsync({ ios: { allowAlert: true, allowSound: true, allowBadge: false } });
    granted = p.granted;
  } catch {
    granted = false;
  }
  write(CHOICE_KEY, granted ? 'on' : 'off');
  track(granted ? 'reminder_enabled' : 'reminder_declined', { from });
  return granted;
}

export async function disableReminders(from: 'result' | 'profile'): Promise<void> {
  write(CHOICE_KEY, 'off');
  await cancelReminder();
  track('reminder_disabled', { from });
}

/** Cancels every pending reminder (practice and first-swing): new analysis, reminders off, account change. */
export async function cancelReminder(): Promise<void> {
  const id = read(PENDING_KEY);
  write(PENDING_KEY, null);
  if (id) await Notifications.cancelScheduledNotificationAsync(id).catch(() => undefined);
  await cancelFirstSwingReminder();
}

export function firstSwingChoice(): string | null {
  return read(FIRST_CHOICE_KEY);
}

/** The golfer has analysed a swing of their own: the "film your first swing" nudge is no longer true. */
export async function cancelFirstSwingReminder(): Promise<void> {
  const id = read(FIRST_PENDING_KEY);
  write(FIRST_PENDING_KEY, null);
  if (id) await Notifications.cancelScheduledNotificationAsync(id).catch(() => undefined);
}

/** "Remind me at my next practice": asks iOS (first time only), then one reminder 2 days later at 6 pm. */
export async function remindFirstSwing(from: 'example' | 'home'): Promise<Date | null> {
  const granted = await enableReminders(from);
  if (!granted) {
    write(FIRST_CHOICE_KEY, 'declined');
    return null;
  }
  try {
    await cancelFirstSwingReminder();
    const date = firstSwingReminderDate(new Date(), TEST_SECONDS);
    const id = await Notifications.scheduleNotificationAsync({
      content: { title: FIRST_SWING_CONTENT.title, body: FIRST_SWING_CONTENT.body, data: { kind: FIRST_KIND } },
      trigger: { type: Notifications.SchedulableTriggerInputTypes.DATE, date },
    });
    write(FIRST_PENDING_KEY, id);
    write(FIRST_CHOICE_KEY, 'scheduled');
    track('reminder_scheduled', { kind: 'first_swing', days: Math.round((date.getTime() - Date.now()) / 86_400_000) });
    return date;
  } catch {
    return null;
  }
}

/** "Not now" on the first-swing offer. Practice reminders after a real result are still offered later. */
export function declineFirstSwing(from: 'example' | 'home'): void {
  write(FIRST_CHOICE_KEY, 'declined');
  track('reminder_declined', { from, kind: 'first_swing' });
}

/** Replaces any pending reminder with one 5 days after this analysis. Returns when it will fire. */
export async function scheduleReminder(input: { analysedAt: Date; fix: string | null; club: string | null }): Promise<Date | null> {
  try {
    await cancelReminder();
    const date = reminderDate(input.analysedAt, TEST_SECONDS);
    if (date.getTime() <= Date.now()) return null;
    const content = reminderContent(input);
    const id = await Notifications.scheduleNotificationAsync({
      content: { title: content.title, body: content.body, data: { kind: KIND } },
      trigger: { type: Notifications.SchedulableTriggerInputTypes.DATE, date },
    });
    write(PENDING_KEY, id);
    track('reminder_scheduled', { days: Math.round((date.getTime() - Date.now()) / 86_400_000) });
    return date;
  } catch {
    return null;
  }
}

/** Tapping a reminder (app open, in the background, or cold-started by the tap) opens Analyse. */
export function useReminderTaps(): void {
  const last = Notifications.useLastNotificationResponse();
  const handled = useRef<string | null>(null);
  useEffect(() => {
    const request = last?.notification.request;
    const kind = request?.content.data?.kind;
    if (!request || (kind !== KIND && kind !== FIRST_KIND) || handled.current === request.identifier) return;
    if (last.actionIdentifier !== Notifications.DEFAULT_ACTION_IDENTIFIER) return;
    handled.current = request.identifier;
    Notifications.clearLastNotificationResponse();   // so it never re-opens Analyse on a later launch
    write(kind === KIND ? PENDING_KEY : FIRST_PENDING_KEY, null);
    track('reminder_opened', { kind: kind === KIND ? 'practice' : 'first_swing' });
    // Next tick: on a cold start the tap arrives while the navigator is still mounting.
    setTimeout(() => router.push('/analyze'), 0);
  }, [last]);
}
