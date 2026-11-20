// Product analytics (PostHog EU), opt-in only. A tiny client instead of the PostHog SDK so nothing is
// collected implicitly: only the named events below, with small non-personal properties, under a
// random analytics id that is not the account id. Nothing is sent until the golfer chooses Allow, or
// at all when no project key is configured. No videos, measurements, names, emails or Apple ids.
// The logic (queue, retry, once-only events, app-open detection) is in analytics-core.ts.
//
// Event dictionary (docs/agent-work/release-1.0.2/ANALYTICS_EVENTS.md):
//   app_opened                cold start, return to foreground after 30 s+, or the moment Allow is tapped
//   signup_completed          Sign in with Apple linked/signed in
//   swing_analysis_started    analysis job created and upload started
//   swing_analysis_completed  once per swing
//   swing_analysis_failed     once per swing
//   paywall_viewed            once per paywall opening
//   trial_started             purchase of a plan with an eligible free trial (instead of subscription_purchased)
//   subscription_purchased    paid purchase confirmed by the server (plan monthly/yearly/lifetime)
//   onboarding_paywall_shown / _dismissed / _trial_started   1.0.7 trial offer in the welcome screens
//                             (trial_started also fires, with source=onboarding)
//   league_joined, drill_started (drill opened), drill_completed (demo video watched to the end)
//   plus: replay_viewed, comparison_used, rank_progressed, progress_viewed, theme_changed,
//         league_viewed, drill_video_started, drill_video_failed, drill_revisited
import * as Application from 'expo-application';
import * as Crypto from 'expo-crypto';
import { Platform } from 'react-native';

import { POSTHOG_HOST, POSTHOG_KEY } from '@/config';

import { createAnalytics, type Consent, type Value } from './analytics-core';

export type { Consent };

export type AnalyticsEvent =
  | 'app_opened' | 'signup_completed'
  | 'swing_analysis_started' | 'swing_analysis_completed' | 'swing_analysis_failed'
  | 'paywall_viewed' | 'trial_started' | 'subscription_purchased'
  | 'league_viewed' | 'league_joined'
  | 'drill_started' | 'drill_completed' | 'drill_video_started' | 'drill_video_failed' | 'drill_revisited'
  | 'replay_viewed' | 'comparison_used' | 'rank_progressed' | 'progress_viewed' | 'theme_changed'
  | 'rating_prompt_requested'
  | 'onboarding_viewed' | 'onboarding_skipped' | 'onboarding_completed'
  | 'example_opened' | 'example_cta_tapped' | 'retry_after_failure'
  | 'paywall_dismissed' | 'save_prompt_viewed'
  | 'recording_started' | 'recording_finished'
  | 'reminder_prompt_viewed' | 'reminder_enabled' | 'reminder_declined' | 'reminder_disabled'
  | 'reminder_scheduled' | 'reminder_opened'
  | 'onboarding_paywall_shown' | 'onboarding_paywall_dismissed' | 'onboarding_paywall_trial_started'
  | 'onboarding_paywall_skipped';

const client = createAnalytics({
  storage: () => globalThis.localStorage ?? null,
  send: async (body) => {
    const res = await fetch(`${POSTHOG_HOST}/batch/`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body });
    return { ok: res.ok, status: res.status };
  },
  now: () => Date.now(),
  newId: () => Crypto.randomUUID(),
  apiKey: POSTHOG_KEY,
  base: () => ({
    app_version: Application.nativeApplicationVersion ?? null,
    build: Application.nativeBuildVersion ?? null,
    $app_version: Application.nativeApplicationVersion ?? null,
    $app_build: Application.nativeBuildVersion ?? null,
    $os: Platform.OS === 'ios' ? 'iOS' : Platform.OS,
    $lib: 'swingguru-app',
  }),
  schedule: (fn, ms) => setTimeout(fn, ms),
  cancel: (handle) => clearTimeout(handle as ReturnType<typeof setTimeout>),
});

export const analyticsAvailable = client.available;
export const consent = (): Consent => client.consent();
export const setConsent = (next: 'granted' | 'denied') => client.setConsent(next);
export const subscribeConsent = (listener: () => void) => client.subscribe(listener);
export const track = (event: AnalyticsEvent, props: Record<string, Value> = {}) => { client.track(event, props); };
/** At most once per key on this device (e.g. one completion per swing id). */
export const trackOnce = (event: AnalyticsEvent, key: string, props: Record<string, Value> = {}) => { client.trackOnce(event, key, props); };
export const trackAppOpened = (source: 'cold_start' | 'foreground') => { client.appOpened(source); };
/** Send now (the app is going to the background). */
export const flushAnalytics = () => client.flush();

/** Sign-out and account deletion: a fresh random id, so later events can't be tied to earlier ones. */
export function resetAnalyticsId() {
  client.resetId();
}

/** The club family, never the exact club. */
export function clubGroup(club: string | null | undefined): string | null {
  if (!club) return null;
  if (/Driver|Wood/.test(club)) return 'woods';
  if (/Hybrid/.test(club)) return 'hybrids';
  if (/Iron/.test(club)) return 'irons';
  if (/Wedge/.test(club)) return 'wedges';
  return 'other';
}

/** Coarse buckets instead of exact values. */
export function scoreBand(total: number | null | undefined): string | null {
  if (total == null) return null;
  const low = Math.floor(total);
  return `${low}-${low + 1}`;
}
