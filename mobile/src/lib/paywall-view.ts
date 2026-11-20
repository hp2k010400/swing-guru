// What the paywall should show, derived from the server status, the offers and the selection.
// Kept pure so the rules (no buy buttons for an active subscriber, restore always reachable,
// neutral copy when eligibility is unknown) are testable without a renderer.

import { SIGN_IN_REQUIRED_COPY, planNote, trialDisclosure, type PlanOffer } from './billing-core';
import type { SettleState } from './billing-flow';
import type { BillingStatus } from './types';

export interface OutcomeCopy {
  tone: 'info' | 'error';
  title: string;
  body: string;
}

/**
 * What to tell the golfer after a purchase/restore has been verified server-side. Never claims the
 * purchase exists when nothing was confirmed (an unavailable check with no SDK entitlement).
 */
export function afterSettleCopy(state: SettleState, sdkEntitled: boolean, kind: 'purchase' | 'restore'): OutcomeCopy | null {
  if (state === 'active') return null;
  if (state === 'identity_changed') {
    return {
      tone: 'error', title: 'Your account changed',
      body: 'Please sign in again and restore purchases.',
    };
  }
  if (state === 'pending' || (state === 'unavailable' && sdkEntitled)) {
    return {
      tone: 'info', title: 'Almost there',
      body: 'Your purchase went through and we are verifying it now. Tap "Check again" in a moment.',
    };
  }
  if (state === 'unavailable') {
    return {
      tone: 'info', title: "Couldn't check your plan",
      body: "We couldn't check your plan just now. Please try again in a moment.",
    };
  }
  if (state === 'sign_in_required') {
    // The account, not the purchase, is what is missing: never nudge toward buying here.
    return { tone: 'info', title: 'Sign-in needed', body: SIGN_IN_REQUIRED_COPY };
  }
  return kind === 'restore'
    ? {
        tone: 'info', title: 'No purchases to restore',
        body: 'We could not find a Swing Guru Pro purchase for this Apple ID. If you bought on another device, make sure you are signed in to the same Apple ID.',
      }
    : {
        tone: 'info', title: 'Not active yet',
        body: "Swing Guru Pro isn't active on your account yet. Tap Check again in a moment, or Restore purchases. If it still doesn't unlock, contact support from the Profile screen.",
      };
}

export interface PaywallView {
  subscribed: boolean;
  showPlans: boolean;
  showBuy: boolean;
  /** Restore stays reachable even when the offerings failed or are empty. */
  showRestore: boolean;
  showManage: boolean;
  ctaLabel: string;
  note: string | null;
  disclosure: string | null;
}

export function ctaLabel(offer: PlanOffer | null): string {
  if (!offer) return 'Choose a plan';
  if (offer.trial) return 'Start free trial';
  if (offer.kind === 'lifetime') return `Buy lifetime access · ${offer.priceString}`;
  return `Subscribe · ${offer.priceString}`;
}

export function paywallView(
  status: BillingStatus | null,
  offers: PlanOffer[],
  selected: PlanOffer | null,
  busy: boolean,
): PaywallView {
  const subscribed = status?.access === 'pro';
  const hasOffers = !subscribed && offers.length > 0;
  return {
    subscribed,
    showPlans: hasOffers,
    // An active subscriber is never offered another purchase from this screen.
    showBuy: hasOffers,
    showRestore: !busy,
    showManage: subscribed,
    ctaLabel: subscribed ? 'Continue' : ctaLabel(selected),
    note: hasOffers && selected ? planNote(selected) : null,
    disclosure: hasOffers && selected ? trialDisclosure(selected) : null,
  };
}

export interface LimitBanner {
  title: string;
  body: string;
  /** Only when a subscription the golfer can buy right now carries a confirmed free trial. */
  trial: string | null;
}

/**
 * Shown on the paywall when the server says this month's free analyses are used up. The reset date
 * is the server's own `allowance_resets_at`; the trial line is only shown when StoreKit confirmed
 * eligibility for a monthly or yearly plan (lifetime never has a trial).
 */
export function limitBanner(status: BillingStatus | null, offers: PlanOffer[]): LimitBanner | null {
  if (!status || !status.enabled || status.access !== 'exhausted') return null;
  const when = status.allowance_resets_at ? new Date(status.allowance_resets_at) : null;
  const date = when && !Number.isNaN(when.getTime())
    ? when.toLocaleDateString(undefined, { day: 'numeric', month: 'long' })
    : null;
  const trialOffer = offers.find((o) => o.kind !== 'lifetime' && o.trial);
  return {
    title: `You've used your ${status.free_total} free analyses this month.`,
    body: `${date ? `Your free analyses reset on ${date}.` : 'Your free analyses reset next month.'} Upgrade to Swing Guru Pro to keep analysing now.`,
    trial: trialOffer ? `Start with a ${trialOffer.trial!.days}-day free trial.` : null,
  };
}
