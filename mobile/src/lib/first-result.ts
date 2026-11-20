// What happens around a golfer's first completed analysis (and the trial offer in onboarding). Pure rules,
// tested in Node.
import type { Eligibility } from './billing-core';

/**
 * Has this golfer already started a free trial? True when this device recorded a trial purchase, when Pro
 * is active (a running trial counts), or when the App Store says this Apple ID is no longer eligible for
 * the subscription's free trial (it has used it, on any device).
 */
export function trialAlreadyStarted(s: { recordedHere: boolean; access: string; eligibilities: Eligibility[] }): boolean {
  return s.recordedHere || s.access === 'pro' || s.eligibilities.includes('ineligible');
}

/**
 * Is this the golfer's first result? Either just finished (opened from Processing with `fresh`), or their
 * only completed swing however it was opened (Home, Library, Progress...). 1.0.7: Processing says "You can
 * leave this screen", and golfers who did then opened the result without `fresh` and never got the
 * first-result offer. `ownCompleted` is null until the swing list has loaded.
 */
export function isFirstResult(s: { fresh: boolean; example: boolean; ownCompleted: number | null }): boolean {
  return !s.example && (s.fresh || s.ownCompleted === 1);
}

/**
 * The full-screen trial paywall opens once, on the first completed result, for golfers without Pro who
 * have never started a trial (1.0.7: someone who started one in onboarding is not asked again).
 */
export function shouldAutoOpenPaywall(s: { enabled: boolean; access: string; seenBefore: boolean; trialStarted?: boolean }): boolean {
  return s.enabled && s.access !== 'pro' && s.access !== 'disabled' && !s.seenBefore && !s.trialStarted;
}

/**
 * 1.0.7: the Pro offer as the last welcome screen, for everyone without Pro who hasn't started a trial on
 * this device. Decided from the server's plan only (fast); the screen itself shows the free trial and its
 * timeline when the App Store confirms this Apple ID is eligible, and plain plans otherwise.
 * Returns null to show it, or why it is skipped (analytics: onboarding_paywall_skipped).
 */
export function onboardingPaywallSkipReason(s: {
  supported: boolean; enabled: boolean; access: string; trialStarted: boolean;
}): 'billing_off' | 'server_billing_off' | 'pro' | 'trial_started' | null {
  if (!s.supported) return 'billing_off';
  if (!s.enabled || s.access === 'disabled') return 'server_billing_off';
  if (s.access === 'pro') return 'pro';
  if (s.trialStarted) return 'trial_started';
  return null;
}

/** "Save this analysis" (Sign in with Apple) is offered on a fresh result to golfers not yet signed in. */
export function shouldOfferSave(s: { fresh: boolean; anonymous: boolean; appleAvailable: boolean; example: boolean }): boolean {
  return s.fresh && s.anonymous && s.appleAvailable && !s.example;
}
