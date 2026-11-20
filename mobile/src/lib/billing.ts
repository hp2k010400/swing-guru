// The RevenueCat binding. All of the flow logic (queue, generation, identity checks, offer
// filtering, server reconciliation) lives in lib/billing-flow.ts, which is dependency-free so it
// can be driven by mocks in tests. This file only supplies the real SDK, session and API.

import { Platform } from 'react-native';
import type { CustomerInfo, PurchasesPackage } from 'react-native-purchases';

import { BILLING_OFF, REVENUECAT_ENTITLEMENT_ID, REVENUECAT_IOS_API_KEY, REVENUECAT_OFFERING_ID, REVENUECAT_PRODUCT_IDS } from '@/config';
import { api } from './api';
import { createBillingFlow, type FlowPackage, type FlowSdk, type OffersResult, type PurchaseResult, type SettleState } from './billing-flow';
import { userId } from './session';
import type { BillingStatus } from './types';

export type { FlowSdk, OffersResult, PurchaseResult, SettleState };

/** A disabled build (no key) or a non-iOS platform simply has no store to talk to. */
export const billingSupported = REVENUECAT_IOS_API_KEY.length > 0 && Platform.OS === 'ios' && !BILLING_OFF;

export const APPLE_MANAGE_URL = 'https://apps.apple.com/account/subscriptions';

function toFlowPackage(pkg: PurchasesPackage): FlowPackage {
  const product = pkg.product;
  const intro = product.introPrice;
  return {
    identifier: pkg.identifier,
    packageType: String(pkg.packageType),
    product: {
      identifier: product.identifier,
      title: product.title,
      priceString: product.priceString,
      price: product.price,
      currencyCode: product.currencyCode,
      subscriptionPeriod: product.subscriptionPeriod ?? null,
      productType: product.productType ? String(product.productType) : null,
      introPrice: intro
        ? {
            price: intro.price,
            priceString: intro.priceString,
            periodUnit: intro.periodUnit,
            periodNumberOfUnits: intro.periodNumberOfUnits,
            cycles: intro.cycles,
          }
        : null,
    },
    raw: pkg,
  };
}

/**
 * react-native-purchases is imported lazily: billing is iOS-only, so on web (and in SSR/static
 * rendering) the native module must never be evaluated at all.
 */
async function defaultSdk(): Promise<FlowSdk> {
  const module = await import('react-native-purchases');
  const purchases = module.default;
  return {
    isConfigured: () => purchases.isConfigured(),
    setLogLevel: (level) => purchases.setLogLevel(level as Parameters<typeof purchases.setLogLevel>[0]),
    configure: (config) => purchases.configure(config),
    logIn: (appUserId) => purchases.logIn(appUserId),
    logOut: () => purchases.logOut(),
    getOfferings: async () => {
      const offerings = await purchases.getOfferings();
      // The "default" offering (verified 2026-10-05: $rc_monthly / $rc_annual / $rc_lifetime) is the
      // current one; fall back to it by name if the dashboard's "current" is ever changed or unset.
      const offering = offerings.current ?? offerings.all?.[REVENUECAT_OFFERING_ID] ?? null;
      return {
        current: offering ? { availablePackages: offering.availablePackages.map(toFlowPackage) } : null,
      };
    },
    purchasePackage: (pkg) => purchases.purchasePackage(pkg as PurchasesPackage),
    restorePurchases: () => purchases.restorePurchases(),
    checkTrialOrIntroductoryPriceEligibility: (ids) => purchases.checkTrialOrIntroductoryPriceEligibility(ids),
  };
}

let injected: FlowSdk | null = null;

function buildFlow() {
  return createBillingFlow({
    supported: billingSupported,
    apiKey: REVENUECAT_IOS_API_KEY,
    entitlementId: REVENUECAT_ENTITLEMENT_ID,
    accepted: {
      monthly: REVENUECAT_PRODUCT_IDS.monthly,
      yearly: REVENUECAT_PRODUCT_IDS.yearly,
      lifetime: REVENUECAT_PRODUCT_IDS.lifetime,
    },
    sdk: () => (injected ? Promise.resolve(injected) : defaultSdk()),
    currentUserId: userId,
    fetchStatus: () => api.billing(),
    sync: () => api.billingSync(),
  });
}

let flow = buildFlow();

/** Test/QA seam only: production code never calls this. */
export function __setBillingSdkForTests(sdk: FlowSdk): void {
  injected = sdk;
  flow = buildFlow(); // fresh queue, generation and configuration state
}

export const configureIdentity = (appUserId: string): Promise<void> => flow.configureIdentity(appUserId);
export const forgetBillingIdentity = (): Promise<void> => flow.forgetIdentity();
export const loadOffers = (appUserId: string): Promise<OffersResult> => flow.loadOffers(appUserId);
export const purchasePlan = (offerId: string): Promise<PurchaseResult> => flow.purchasePlan(offerId);
export const restorePlan = (): Promise<PurchaseResult> => flow.restorePlan();
export const settlePurchase = (appUserId: string, sdkEntitled: boolean) => flow.settle(appUserId, sdkEntitled);
export const serverStatus = (): Promise<BillingStatus> => flow.status();
export const syncEntitlement = (): Promise<BillingStatus> => api.billingSync();

export function sdkEntitled(info: CustomerInfo | null | undefined): boolean {
  return Boolean(info?.entitlements?.active?.[REVENUECAT_ENTITLEMENT_ID]);
}

export function manageUrl(status: BillingStatus | null): string {
  return status?.management_url || APPLE_MANAGE_URL;
}
