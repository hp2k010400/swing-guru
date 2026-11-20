// Practice reminders (1.0.6): one local notification, 5 days after the golfer's latest analysis, nudging
// them to film again and see if their biggest fix improved. Pure, so the timing and copy are tested.
// Scheduled on the phone only: no server, no push token, nothing leaves the device.

export const REMIND_AFTER_DAYS = 5;
/** Never buzz early in the morning or late at night: outside these hours the reminder moves to 6 pm. */
export const QUIET_BEFORE_HOUR = 9;
export const QUIET_FROM_HOUR = 20;
export const EVENING_HOUR = 18;

/**
 * Test builds only: fire after this many seconds instead of 5 days. Honoured only when the build talks to
 * the staging server, so a production build can never ship a 1-minute reminder even if the env leaks.
 */
export function reminderTestSeconds(envValue: string | undefined, apiUrl: string): number {
  const seconds = Number(envValue);
  if (!Number.isFinite(seconds) || seconds <= 0) return 0;
  return /staging/i.test(apiUrl) ? Math.round(seconds) : 0;
}

/** When to remind: 5 days after `analysedAt`, moved to 6 pm that day if it would land in quiet hours. */
export function reminderDate(analysedAt: Date, testSeconds = 0): Date {
  if (testSeconds > 0) return new Date(analysedAt.getTime() + testSeconds * 1000);
  const at = new Date(analysedAt.getTime());
  at.setDate(at.getDate() + REMIND_AFTER_DAYS);
  const hour = at.getHours();
  if (hour < QUIET_BEFORE_HOUR || hour >= QUIET_FROM_HOUR) at.setHours(EVENING_HOUR, 0, 0, 0);
  return at;
}

export function reminderContent(input: { fix: string | null; club: string | null }): { title: string; body: string } {
  const club = input.club ? input.club.toLowerCase() : 'swing';
  const what = input.club ? `${club} swing` : 'swing';
  if (input.fix) {
    return {
      title: 'Time to re-check your swing',
      body: `Been practising your ${input.fix.toLowerCase()}? Film your ${what} and see if your score has gone up.`,
    };
  }
  return { title: 'Time to re-check your swing', body: `Film your ${what} and see if your score has gone up.` };
}

/** "Friday" or "today"/"tomorrow", for the card that confirms the reminder. */
export function reminderDayLabel(when: Date, now: Date): string {
  const day = (d: Date) => Math.floor(new Date(d.getFullYear(), d.getMonth(), d.getDate()).getTime() / 86_400_000);
  const diff = day(when) - day(now);
  if (diff <= 0) return 'today';
  if (diff === 1) return 'tomorrow';
  return when.toLocaleDateString('en-GB', { weekday: 'long' });
}

// First-swing reminder (1.0.6): for golfers who finished onboarding or watched the example but haven't
// analysed a swing of their own. One local notification, 2 days later at 6 pm.
export const FIRST_SWING_AFTER_DAYS = 2;
export const FIRST_SWING_CONTENT = {
  title: 'Ready for your first swing score?',
  body: 'At the range this week? Film one swing and get your score in a minute.',
} as const;

/** 2 days after `from`, at 6 pm (test builds: `testSeconds` from now). */
export function firstSwingReminderDate(from: Date, testSeconds = 0): Date {
  if (testSeconds > 0) return new Date(from.getTime() + testSeconds * 1000);
  const at = new Date(from.getTime());
  at.setDate(at.getDate() + FIRST_SWING_AFTER_DAYS);
  at.setHours(EVENING_HOUR, 0, 0, 0);
  return at;
}

/**
 * Offer "Remind me at my next practice" only to a golfer with no analysed swings of their own (count known),
 * who hasn't answered this offer yet, hasn't turned reminders off, and whom iOS still lets us ask.
 */
export function shouldOfferFirstSwingReminder(input: {
  ownSwings: number | null; permission: ReminderPermission; choice: 'on' | 'off' | null; firstChoice: string | null;
}): boolean {
  return input.ownSwings === 0 && input.firstChoice === null && input.choice !== 'off' && input.permission !== 'denied';
}

export type ReminderPermission = 'granted' | 'denied' | 'undetermined';

/** Show the "Remind me" card only on a fresh own result, when the golfer hasn't chosen yet. */
export function shouldOfferReminder(input: { fresh: boolean; example: boolean; permission: ReminderPermission; choice: 'on' | 'off' | null }): boolean {
  return input.fresh && !input.example && input.choice === null && input.permission !== 'denied';
}

/** Schedule after a fresh own result only when the golfer turned reminders on and iOS allows them. */
export function shouldSchedule(input: { fresh: boolean; example: boolean; permission: ReminderPermission; choice: 'on' | 'off' | null }): boolean {
  return input.fresh && !input.example && input.choice === 'on' && input.permission === 'granted';
}
