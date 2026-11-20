// Pure billing decisions: no React, no RevenueCat import, no network. The screen and the SDK
// wrapper both lean on this, and it is what tests exercise directly (see mobile/tests).
//
// Two rules shape all of it: a price is only ever the store's localized string, and a free-trial
// claim is only ever made when the product really carries a free two-week introductory offer *and*
// StoreKit says this customer is eligible for it.

import type { BillingStatus } from './types';

export type PlanKind = 'monthly' | 'yearly' | 'lifetime' | 'other';
export type Eligibility = 'eligible' | 'ineligible' | 'unknown' | 'no_offer';
export type ReconcileState = 'active' | 'pending' | 'free' | 'in_progress' | 'sign_in_required'
  | 'exhausted' | 'disabled' | 'unknown';

/**
 * Free analyses are three per calendar month and need an account that survives a reinstall, so the
 * golfer sees one neutral sentence for the auth state - never a purchase prompt.
 */
export const SIGN_IN_REQUIRED_COPY =
  'Free analyses need an account that stays with you. Sign in with Apple to continue. It keeps your swings and free analyses if you reinstall.';

/** The month's slots are held by analyses that have not finished: wait, do not buy. */
export const ANALYSES_IN_PROGRESS_COPY =
  "Your earlier free analyses are still finishing. We'll start this one as soon as a slot frees up.";


export interface TrialCopy {
  days: number;
  /** Store-formatted zero price ("£0.00") - never a hardcoded free string. */
  priceString: string;
}

export interface PlanOffer {
  /** Package identifier from RevenueCat's current offering. */
  id: string;
  kind: PlanKind;
  title: string;
  /** Localized store price, e.g. "£9.99". */
  priceString: string;
  /** Numeric store price and ISO currency, for honest comparisons; null when the store did not say. */
  price: number | null;
  currencyCode: string | null;
  /** The plan's value line ("£3.33 a month, billed yearly"), from real store prices; see withValueLines. */
  value: string | null;
  /** Human period for the renewal sentence, or null for the one-time purchase. */
  period: 'month' | 'year' | null;
  trial: TrialCopy | null;
  /** "Then £9.99 per month, auto-renewing" / null for lifetime. */
  renewalCopy: string | null;
  badge: string | null;
  /** What StoreKit said about the introductory offer for this customer. */
  eligibility: Eligibility;
}

export interface ProductLike {
  identifier: string;
  title: string;
  priceString: string;
  price?: number | null;
  currencyCode?: string | null;
  /** ISO 8601 subscription period ("P1M", "P1Y"); null/undefined for a one-time purchase. */
  subscriptionPeriod?: string | null;
  /** PRODUCT_TYPE from the SDK, when the store can tell us ("NON_CONSUMABLE", ...). */
  productType?: string | null;
  introPrice?: {
    price: number;
    priceString: string;
    periodUnit: string;
    periodNumberOfUnits: number;
    cycles?: number | null;
  } | null;
}

export interface PackageLike {
  identifier: string;
  packageType: string;
  product: ProductLike;
}

/** The three products this app sells; anything else must not be offered. */
export interface AcceptedProducts {
  monthly: string;
  yearly: string;
  lifetime: string;
}

export const DEFAULT_ACCEPTED_PRODUCTS: AcceptedProducts = {
  monthly: 'com.swingguru.app.pro.monthly',
  yearly: 'com.swingguru.app.pro.yearly',
  lifetime: 'com.swingguru.app.pro.lifetime',
};

function expectedPeriodFor(kind: PlanKind): string | null {
  return kind === 'monthly' ? 'P1M' : kind === 'yearly' ? 'P1Y' : null;
}

function acceptedIdFor(kind: PlanKind, accepted: AcceptedProducts): string | null {
  if (kind === 'monthly') return accepted.monthly;
  if (kind === 'yearly') return accepted.yearly;
  if (kind === 'lifetime') return accepted.lifetime;
  return null;
}

/** The subset of the SDK's PACKAGE_TYPE values this app maps. */
export function kindOf(packageType: string): PlanKind {
  switch (packageType) {
    case 'MONTHLY':
      return 'monthly';
    case 'ANNUAL':
    case 'YEARLY':
      return 'yearly';
    case 'LIFETIME':
      return 'lifetime';
    default:
      return 'other';
  }
}

/**
 * ``INTRO_ELIGIBILITY_STATUS``: 0 unknown, 1 ineligible, 2 eligible, 3 no intro offer.
 * Unknown must read as "do not show a trial", never as "probably eligible".
 */
export function eligibilityOf(status: number | undefined): Eligibility {
  switch (status) {
    case 2:
      return 'eligible';
    case 1:
      return 'ineligible';
    case 3:
      return 'no_offer';
    default:
      return 'unknown';
  }
}

/**
 * A trial is offered only when the product's own intro price is free for two weeks and the
 * customer is confirmed eligible. Everything else gets the plain purchase call to action.
 */
export function trialFor(product: ProductLike, eligibility: Eligibility): TrialCopy | null {
  const intro = product.introPrice;
  if (!intro || eligibility !== 'eligible') return null;
  if (intro.price !== 0) return null;
  // A free trial is a single discounted period; a multi-period discount is not a free trial.
  if (intro.cycles !== undefined && intro.cycles !== null && intro.cycles !== 1) return null;
  // The length comes from the App Store's own offer (1.0.8: the live offer may be 7 days, not 14),
  // in days or weeks only, so the screen never claims a length Apple doesn't give.
  const unit = (intro.periodUnit ?? '').toUpperCase();
  const n = intro.periodNumberOfUnits;
  if (typeof n !== 'number' || !Number.isInteger(n) || n < 1) return null;
  const days = unit === 'DAY' ? n : unit === 'WEEK' ? n * 7 : null;
  if (!days || days > 31) return null;
  return { days, priceString: intro.priceString };
}

/** 1.0.8 yearly card headline for an eligible golfer: "7 days free". */
export function trialHeadline(offer: PlanOffer): string | null {
  return offer.trial ? `${offer.trial.days} days free` : null;
}

/** The price line under the trial headline: "then $59.99/year". */
export function trialPriceLine(offer: PlanOffer): string | null {
  if (!offer.trial || !offer.period) return null;
  return `then ${offer.priceString}/${offer.period}`;
}

function renewalCopy(kind: PlanKind, priceString: string): string | null {
  if (kind === 'monthly') return `Then ${priceString} per month, auto-renewing`;
  if (kind === 'yearly') return `Then ${priceString} per year, auto-renewing`;
  return null; // lifetime is a one-time purchase
}

function titleFor(kind: PlanKind, product: ProductLike): string {
  if (kind === 'monthly') return product.title || 'Monthly';
  if (kind === 'yearly') return product.title || 'Yearly';
  if (kind === 'lifetime') return product.title || 'Lifetime';
  return product.title || 'Plan';
}

/**
 * Build the buyable offer for one package, or null when the store did not give us a real
 * localized price, or when the package is not one of the three accepted products with the
 * expected type/period (the paywall shows "unavailable" rather than selling something the
 * backend will refuse).
 */
export function offerFromPackage(pkg: PackageLike, eligibility: Eligibility,
                                 accepted: AcceptedProducts = DEFAULT_ACCEPTED_PRODUCTS): PlanOffer | null {
  const kind = kindOf(pkg.packageType);
  const expected = acceptedIdFor(kind, accepted);
  if (!expected || pkg.product?.identifier !== expected) return null;
  if (kind !== 'lifetime') {
    const period = (pkg.product.subscriptionPeriod ?? '').trim().toUpperCase();
    // StoreKit 1 cannot always report the period; when it does, it must match the plan.
    if (period && period !== expectedPeriodFor(kind)) return null;
  } else if (pkg.product.productType && pkg.product.productType !== 'NON_CONSUMABLE') {
    return null;
  }
  const price = (pkg.product?.priceString ?? '').trim();
  if (!price) return null;
  return {
    id: pkg.identifier,
    kind,
    title: titleFor(kind, pkg.product),
    priceString: price,
    price: typeof pkg.product.price === 'number' && Number.isFinite(pkg.product.price) && pkg.product.price > 0 ? pkg.product.price : null,
    currencyCode: pkg.product.currencyCode ? String(pkg.product.currencyCode) : null,
    value: kind === 'lifetime' ? 'Pay once, keep Pro forever. No subscription.' : null,
    period: kind === 'monthly' ? 'month' : kind === 'yearly' ? 'year' : null,
    trial: kind === 'lifetime' ? null : trialFor(pkg.product, eligibility),
    // Lifetime never renews, so it never carries a renewal sentence.
    renewalCopy: renewalCopy(kind, price),
    badge: kind === 'yearly' ? 'Best value' : kind === 'lifetime' ? 'Pay once' : null,
    eligibility,
  };
}

/** Yearly (best value) first, then lifetime, then monthly; anything else is ignored on this paywall. */
const ORDER: Record<PlanKind, number> = { yearly: 0, lifetime: 1, monthly: 2, other: 9 };

/** A store amount in the store's own currency, or null when it cannot be formatted honestly. */
export function formatMoney(amount: number, currencyCode: string | null): string | null {
  if (!currencyCode || !Number.isFinite(amount)) return null;
  try {
    return new Intl.NumberFormat(undefined, { style: 'currency', currency: currencyCode }).format(amount);
  } catch {
    return null;
  }
}

/**
 * Value lines from the real store prices only: the yearly plan's monthly equivalent and its saving
 * against twelve months of the monthly plan. Nothing is claimed when either price is missing or the
 * two are in different currencies, and the percentage is rounded down so it never overstates.
 */
export function withValueLines(offers: PlanOffer[]): PlanOffer[] {
  const monthly = offers.find((o) => o.kind === 'monthly');
  return offers.map((offer) => {
    if (offer.kind === 'monthly') return { ...offer, value: 'Flexible. Cancel any time.' };
    if (offer.kind !== 'yearly' || offer.price === null) return offer;
    const perMonth = formatMoney(Math.round((offer.price / 12) * 100) / 100, offer.currencyCode);
    if (!perMonth) return offer;
    // 1.0.8: just "Save 49% vs monthly" (the old line's saving amount read like the yearly price).
    if (monthly && monthly.price !== null && monthly.currencyCode === offer.currencyCode) {
      const twelve = monthly.price * 12;
      const pct = Math.floor(((twelve - offer.price) / twelve) * 100);
      if (pct > 0) return { ...offer, value: `Save ${pct}% vs monthly` };
    }
    return { ...offer, value: `${perMonth} a month, billed yearly.` };
  });
}

export function offersFromPackages(
  packages: PackageLike[],
  eligibilityByProduct: Record<string, number | undefined>,
  accepted: AcceptedProducts = DEFAULT_ACCEPTED_PRODUCTS,
): PlanOffer[] {
  const offers: PlanOffer[] = [];
  for (const pkg of packages) {
    // Anything this paywall does not sell (a weekly package, say) is left out entirely.
    if (kindOf(pkg.packageType) === 'other') continue;
    const eligibility = eligibilityOf(eligibilityByProduct[pkg.product?.identifier ?? '']);
    const offer = offerFromPackage(pkg, eligibility, accepted);
    if (offer) offers.push(offer);
  }
  return withValueLines(offers.sort((a, b) => ORDER[a.kind] - ORDER[b.kind]));
}

/** The plan row's sub-line: never a categorical claim when eligibility is unknown. */
export function planNote(offer: PlanOffer): string {
  if (offer.kind === 'lifetime') return 'One-time purchase · no trial';
  if (offer.trial) return `${offer.trial.days}-day free trial available`;
  if (offer.eligibility === 'unknown') return 'Trial eligibility not confirmed';
  return 'No free trial';
}

/** The disclosure under the buy button, in the store's own localized price. */
export function trialDisclosure(offer: PlanOffer): string {
  const period = offer.kind === 'yearly' ? 'per year' : 'per month';
  if (offer.trial) {
    return `Free for ${offer.trial.days} days, then ${offer.priceString} ${period}. Cancel any time in the App Store; auto-renews until cancelled.`;
  }
  if (offer.kind === 'lifetime') {
    return `One payment of ${offer.priceString}. Not a subscription, so there is no renewal and no free trial.`;
  }
  return `${offer.priceString} ${period}, auto-renewing until cancelled.`;
}

export function paywallReady(offers: PlanOffer[]): boolean {
  return offers.length > 0;
}

export interface PurchaseErrorState {
  /** User dismissed the sheet: stay quiet, no error copy. */
  cancelled: boolean;
  /** Ask-to-buy / SCA: the purchase is not complete yet. */
  pending: boolean;
  message: string | null;
}

/**
 * ``PURCHASES_ERROR_CODE``: "1" is a user cancellation, "20" a deferred/ask-to-buy purchase.
 * A cancelled purchase must never look like a failure or a success.
 */
export function purchaseErrorState(error: { code?: unknown; userCancelled?: unknown } | null | undefined): PurchaseErrorState {
  const code = error?.code === undefined || error?.code === null ? '' : String(error.code);
  if (code === '1' || error?.userCancelled === true) return { cancelled: true, pending: false, message: null };
  if (code === '20') {
    return { cancelled: false, pending: true, message: 'Your purchase is waiting for approval. We will unlock it as soon as it is confirmed.' };
  }
  // No claim about whether the store charged anything: an unknown failure may or may not have.
  return { cancelled: false, pending: false, message: "That didn't go through. Please try again." };
}

/**
 * What the app should show after a purchase/restore: the *server* is authoritative, so a fresh
 * SDK entitlement that the backend has not confirmed yet is "pending", never "active".
 */
export function reconcile(server: BillingStatus | null, sdkEntitled: boolean): ReconcileState {
  if (!server) return 'unknown';
  if (!server.enabled) return 'disabled';
  if (server.access === 'pro') return 'active';
  // A purchase that the store already granted is only ever "pending", even on an anonymous account.
  if (sdkEntitled) return 'pending';
  if (server.access === 'sign_in_required') return 'sign_in_required';
  if (server.access === 'in_progress') return 'in_progress';
  if (server.access === 'exhausted') return 'exhausted';
  return 'free';
}

/** "2 free analyses left this month" - null when there is nothing honest to say. */
/** "1 analysis" / "3 analyses" (the free allowance is a server setting, so it can be 1). */
export function analysesCount(n: number): string {
  return n === 1 ? '1 analysis' : `${n} analyses`;
}

export function freeLeftCopy(server: BillingStatus | null): string | null {
  if (!server || !server.enabled || server.access === 'pro' || server.access === 'sign_in_required') return null;
  if (server.free_remaining <= 0) return null;
  if (server.free_remaining === 1 && server.free_total === 1) return '1 free analysis this month';
  if (server.free_remaining === 1) return 'Your last free analysis this month';
  return `${server.free_remaining} free analyses left this month`;
}

/** When the next month's free analyses start, in the golfer's locale - null when there is nothing to say. */
export function resetCopy(server: BillingStatus | null): string | null {
  if (!server || !server.enabled || server.access === 'pro' || !server.allowance_resets_at) return null;
  const when = new Date(server.allowance_resets_at);
  if (Number.isNaN(when.getTime())) return null;
  return `Free analyses reset on ${when.toLocaleDateString(undefined, { day: 'numeric', month: 'long' })}`;
}

export function priceLabel(offer: PlanOffer): string {
  return offer.kind === 'lifetime' ? `${offer.priceString} once` : offer.priceString;
}

/**
 * The honest trial timeline shown on the paywall (Blinkist-style): what happens today, how to avoid
 * being charged, and exactly when and what the first charge is. No reminder is promised because the
 * app doesn't send one; cancelling in the App Store before the last day is the real, stated way out.
 */
export function trialTimeline(offer: PlanOffer | null): { when: string; what: string }[] | null {
  if (!offer?.trial || !offer.period) return null;
  const days = offer.trial.days;
  return [
    { when: 'Today', what: 'Full Swing Guru Pro, free' },
    { when: `Before day ${days}`, what: 'Cancel any time in your App Store settings and you pay nothing' },
    { when: `Day ${days}`, what: `${offer.priceString} per ${offer.period} starts, renewing until you cancel` },
  ];
}
