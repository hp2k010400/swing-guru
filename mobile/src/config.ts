// Build-time configuration (EXPO_PUBLIC_* values are embedded in the app bundle - never put secrets here).
export const API_URL = (process.env.EXPO_PUBLIC_API_URL ?? 'http://127.0.0.1:8790').replace(/\/$/, '');
export const SUPABASE_URL = process.env.EXPO_PUBLIC_SUPABASE_URL ?? '';
export const SUPABASE_ANON_KEY = process.env.EXPO_PUBLIC_SUPABASE_ANON_KEY ?? '';
// "supabase" in every real build; "dev" only against a local development service.
export const AUTH_MODE: 'supabase' | 'dev' =
  process.env.EXPO_PUBLIC_AUTH_MODE === 'dev' || !SUPABASE_URL ? 'dev' : 'supabase';
// The live Swing Guru site (Netlify, site/ in the repo). swingguru.app is not registered yet; once it
// is attached to the same Netlify site these addresses keep working, so builds never break.
export const SITE_URL = 'https://swingguru-app.netlify.app';
// EXPO_PUBLIC_STAGING_KEY: staging builds only (eas.json "staging" profile); release builds leave it
// unset and talk to production. EXPO_PUBLIC_OVERLAY_VERSION: the replay overlay the viewer is asked
// for - "2" in the production profile (Build 12), "3" in the staging profile (overlay v3, development).
//   EXPO_PUBLIC_STAGING_KEY      shared key the staging server requires on signed-in requests
//   EXPO_PUBLIC_OVERLAY_VERSION  "2" = overlay v2, "3" = overlay v3, unset = v1
export const STAGING_KEY = process.env.EXPO_PUBLIC_STAGING_KEY ?? '';
export const OVERLAY_VERSION = process.env.EXPO_PUBLIC_OVERLAY_VERSION ?? '';
export const IS_STAGING = STAGING_KEY.length > 0;
// "off" (staging builds) disables in-app purchases entirely, even when a RevenueCat key is present.
export const BILLING_OFF = process.env.EXPO_PUBLIC_BILLING === 'off';
export const PRIVACY_URL = process.env.EXPO_PUBLIC_PRIVACY_URL ?? `${SITE_URL}/privacy`;
export const SUPPORT_URL = process.env.EXPO_PUBLIC_SUPPORT_URL ?? `${SITE_URL}/support`;
// RevenueCat's *public* iOS SDK key (appl_...). Public by design - a secret key must never be
// compiled into the app. An empty value ships a billing-disabled build: the paywall stays closed
// and every analysis path keeps working.
export const REVENUECAT_IOS_API_KEY = process.env.EXPO_PUBLIC_REVENUECAT_IOS_API_KEY ?? '';
// The single entitlement in the RevenueCat project that unlocks analysis. Not a secret.
// The RevenueCat offering the paywall sells (packages $rc_monthly, $rc_annual, $rc_lifetime).
export const REVENUECAT_OFFERING_ID = process.env.EXPO_PUBLIC_REVENUECAT_OFFERING_ID ?? 'default';
export const REVENUECAT_ENTITLEMENT_ID = process.env.EXPO_PUBLIC_REVENUECAT_ENTITLEMENT_ID ?? 'pro';
// The three accepted products. The paywall only offers these ids (with the matching period), so a
// misconfigured package can never be sold and then refused by the server.
export const REVENUECAT_PRODUCT_IDS = {
  monthly: process.env.EXPO_PUBLIC_REVENUECAT_MONTHLY_PRODUCT_ID ?? 'com.swingguru.app.pro.monthly',
  yearly: process.env.EXPO_PUBLIC_REVENUECAT_YEARLY_PRODUCT_ID ?? 'com.swingguru.app.pro.yearly',
  lifetime: process.env.EXPO_PUBLIC_REVENUECAT_LIFETIME_PRODUCT_ID ?? 'com.swingguru.app.pro.lifetime',
} as const;
// PostHog EU project key (phc_..., public by design). Empty = analytics off entirely.
export const POSTHOG_KEY = process.env.EXPO_PUBLIC_POSTHOG_KEY ?? '';
export const POSTHOG_HOST = 'https://eu.i.posthog.com';
// Terms of Use (site/terms.html): the subscription terms, linking Apple's Standard EULA.
export const TERMS_URL = process.env.EXPO_PUBLIC_TERMS_URL ?? `${SITE_URL}/terms`;
