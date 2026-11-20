// The paywall: three plans, App Store prices, and a trial that is only claimed when StoreKit says
// this customer is eligible and the product really carries a free fortnight. The server verifies
// every purchase before analysis unlocks (lib/billing.ts), so this screen never grants access.
import { router, useFocusEffect, useLocalSearchParams } from 'expo-router';
import { SymbolView, type SFSymbol } from 'expo-symbols';
import * as WebBrowser from 'expo-web-browser';
import { useCallback, useEffect, useRef, useState } from 'react';
import { ActivityIndicator, Alert, Pressable, ScrollView, StyleSheet, Text, View } from 'react-native';
import { useSafeAreaInsets } from 'react-native-safe-area-context';

import { Button, Caption } from '@/components/ui';
import { PRIVACY_URL, TERMS_URL } from '@/config';
import { track } from '@/lib/analytics';
import { ApiError, friendly } from '@/lib/api';
import {
  billingSupported, loadOffers, manageUrl, purchasePlan, restorePlan, serverStatus, settlePurchase,
} from '@/lib/billing';
import {
  analysesCount, priceLabel, planNote, trialHeadline, trialPriceLine, trialTimeline, type PlanOffer,
} from '@/lib/billing-core';
import { billingLog } from '@/lib/billing-log';
import { afterSettleCopy, limitBanner, paywallView } from '@/lib/paywall-view';
import { userId } from '@/lib/session';
import { markTrialStarted } from '@/lib/trial-flag';
import type { BillingStatus } from '@/lib/types';
import { colors, radius, space, type, themed } from '@/theme';

type Notice = { tone: 'info' | 'error' | 'good'; text: string } | null;

const VERIFYING_MESSAGE = 'Your purchase went through and we are verifying it now. Tap "Check again" in a moment.';
const IDENTITY_MESSAGE = 'Your account changed during that step. Please sign in again and restore purchases.';

/** Where the paywall was opened from: onboarding | first_result | limit | profile | other. */
function paywallSource(from: string | undefined): string {
  return from === 'onboarding' || from === 'first_result' || from === 'limit' || from === 'profile' ? from : 'other';
}

/** 1.0.7 onboarding trial offer: the three Pro benefits. */
const ONBOARDING_BENEFITS: { icon: SFSymbol; text: string }[] = [
  { icon: 'infinity', text: 'Unlimited swing analyses' },
  { icon: 'figure.golf', text: 'Personal drills for every fault' },
  { icon: 'chart.line.uptrend.xyaxis', text: 'Progress tracking and leagues' },
];

export default function Paywall() {
  // `fix`: the golfer's biggest fix from the result that opened this (first_result only).
  const { from, fix } = useLocalSearchParams<{ from?: string; fix?: string }>();
  const source = paywallSource(from);
  const onboarding = source === 'onboarding';
  /** "Not now" / close: back to the welcome screen's last step (onboarding) or wherever it was opened from. */
  const dismiss = () => {
    if (source === 'first_result' || onboarding) track('paywall_dismissed', { source });
    if (onboarding) track('onboarding_paywall_dismissed', {});
    router.back();
  };
  const [status, setStatus] = useState<BillingStatus | null>(null);
  const [offers, setOffers] = useState<PlanOffer[]>([]);
  const [unavailable, setUnavailable] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [chosen, setChosen] = useState<string | null>(null);
  const [busy, setBusy] = useState<'purchase' | 'restore' | null>(null);
  const [notice, setNotice] = useState<Notice>(null);
  const loadedFor = useRef<string | null>(null);
  // The device's real safe-area insets from the root provider. A SafeAreaView measured inside this
  // full-screen modal can read a zero top inset (it mounts while the modal is still sliding up),
  // which put the close button under the status bar.
  const insets = useSafeAreaInsets();

  /** Load (or re-load) the server status and the store's plans. Returns what it found. */
  const refresh = useCallback(async (): Promise<'active' | 'ready' | 'unavailable'> => {
    setLoading(true);
    setNotice(null);
    try {
      const who = await userId();
      if (loadedFor.current !== who) {
        // A different account: never show the previous account's status or plans.
        loadedFor.current = who;
        setStatus(null);
        setOffers([]);
        setChosen(null);
      }
      const server = await serverStatus();
      setStatus(server);
      if (!server.enabled) {
        setOffers([]);
        setUnavailable('Subscriptions are not switched on for this app yet.');
        return 'ready';
      }
      if (server.access === 'pro') {
        setOffers([]);
        setUnavailable(null);
        return 'active';
      }
      const result = await loadOffers(who);
      setOffers(result.offers);
      setUnavailable(result.unavailable);
      setChosen((current) => current ?? result.offers.find((o) => o.kind === 'yearly')?.id ?? result.offers[0]?.id ?? null);
      return 'ready';
    } catch (error) {
      setOffers([]);
      setStatus(null);
      setUnavailable(
        error instanceof ApiError && error.code === 'billing_unavailable'
          ? "We couldn't check your plan just now. Please try again in a moment."
          : friendly(error),
      );
      return 'unavailable';
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    track('paywall_viewed', { source });
    if (source === 'onboarding') track('onboarding_paywall_shown', {});
  }, [source]);

  // Refresh on focus so returning from the App Store sheet (or from Manage) re-checks the server.
  useFocusEffect(
    useCallback(() => {
      void refresh();
    }, [refresh]),
  );

  /** "Check again": only a server-confirmed active plan closes the paywall. */
  async function checkAgain() {
    if ((await refresh()) === 'active') router.back();
  }

  /**
   * Ask the server for the last word, then move to the right state. When the App Store already
   * granted the entitlement but the server's check has not caught up, re-check a few times before
   * showing "Check again" (never unlocks on the device's word alone).
   */
  async function afterPurchase(who: string, entitled: boolean, kind: 'purchase' | 'restore') {
    let { state, status: server } = await settlePurchase(who, entitled);
    for (let attempt = 1; state === 'pending' && attempt <= 3; attempt += 1) {
      billingLog('settle retry', { attempt, kind });
      await new Promise((resolve) => setTimeout(resolve, 1500));
      ({ state, status: server } = await settlePurchase(who, entitled));
    }
    billingLog('settled', { kind, state, sdkEntitled: entitled, access: server?.access ?? null });
    if (state === 'identity_changed') {
      await refresh();
      setNotice({ tone: 'error', text: afterSettleCopy(state, entitled, kind)?.body ?? IDENTITY_MESSAGE });
      return;
    }
    if (server) setStatus(server);
    const copy = afterSettleCopy(state, entitled, kind);
    if (!copy) {
      if (kind === 'purchase') {
        // Server-verified. A plan bought with an eligible free trial is a trial start, not yet revenue.
        const offer = offers.find((o) => o.id === chosen);
        track(offer?.trial ? 'trial_started' : 'subscription_purchased', {
          plan: offer?.kind ?? null, trial_days: offer?.trial?.days ?? null,
          source, free_used: status?.free_used ?? null, free_total: status?.free_total ?? null,
        });
        if (offer?.trial) {
          markTrialStarted();   // never shown the post-first-score trial offer again
          if (onboarding) track('onboarding_paywall_trial_started', { plan: offer.kind, trial_days: offer.trial.days });
        }
      }
      // Verified by the server: Pro is active. Close the paywall; every screen re-reads the plan on focus.
      if (kind === 'restore') Alert.alert('Purchases restored', 'Swing Guru Pro is active on this account.');
      router.back();
      return;
    }
    setNotice({ tone: copy.tone === 'error' ? 'error' : 'info', text: copy.body });
  }

  async function buy() {
    if (!chosen) return;
    setBusy('purchase');
    setNotice(null);
    try {
      const who = await userId();
      const result = await purchasePlan(chosen);
      billingLog('purchase result', { outcome: result.outcome, sdkEntitled: result.entitled });
      if (result.outcome === 'cancelled') return; // quiet, as Apple requires
      if (result.outcome === 'pending') {
        setNotice({ tone: 'info', text: result.message ?? VERIFYING_MESSAGE });
        return;
      }
      if (result.outcome === 'failed') {
        setNotice({ tone: 'error', text: result.message ?? "That didn't go through. Please try again." });
        return;
      }
      if (result.outcome === 'unavailable') {
        setNotice({ tone: 'info', text: result.message ?? 'Purchases are not available in this build.' });
        return;
      }
      if (result.outcome === 'identity_changed') {
        await refresh();
        setNotice({ tone: 'error', text: result.message ?? IDENTITY_MESSAGE });
        return;
      }
      await afterPurchase(who, result.entitled, 'purchase');
    } catch (error) {
      setNotice({ tone: 'error', text: friendly(error) });
    } finally {
      setBusy(null);
    }
  }

  async function restore() {
    setBusy('restore');
    setNotice(null);
    try {
      const who = await userId();
      const result = await restorePlan();
      billingLog('restore result', { outcome: result.outcome, sdkEntitled: result.entitled });
      if (result.outcome === 'cancelled') return;
      if (result.outcome === 'pending') {
        setNotice({ tone: 'info', text: result.message ?? VERIFYING_MESSAGE });
        return;
      }
      if (result.outcome === 'failed') {
        setNotice({ tone: 'error', text: result.message ?? "We couldn't restore purchases. Please try again." });
        return;
      }
      if (result.outcome === 'unavailable') {
        setNotice({ tone: 'info', text: result.message ?? 'Purchases are not available in this build.' });
        return;
      }
      if (result.outcome === 'identity_changed') {
        await refresh();
        setNotice({ tone: 'error', text: result.message ?? IDENTITY_MESSAGE });
        return;
      }
      await afterPurchase(who, result.entitled, 'restore');
    } catch (error) {
      setNotice({ tone: 'error', text: friendly(error) });
    } finally {
      setBusy(null);
    }
  }

  const selected = offers.find((o) => o.id === chosen) ?? null;
  const view = paywallView(status, offers, selected, busy !== null);
  const limit = loading ? null : limitBanner(status, offers);

  return (
    <View style={[styles.screen, { paddingTop: insets.top, paddingBottom: insets.bottom }]}>
      <View style={styles.header}>
        <Pressable accessibilityRole="button" accessibilityLabel={onboarding ? 'Not now' : 'Close'} onPress={dismiss} hitSlop={8}
          style={({ pressed }) => [styles.round, pressed && { opacity: 0.6 }]}>
          <SymbolView name="xmark" size={16} tintColor={colors.ink} />
        </Pressable>
        <Text style={[type.headline, { color: colors.ink }]}>{onboarding ? 'Swing Guru Pro' : 'Plans'}</Text>
        <View style={{ width: 44 }} />
      </View>

      <ScrollView contentContainerStyle={{ paddingBottom: 28 }} showsVerticalScrollIndicator={false}>
        {limit ? (
          <View style={styles.limit} accessibilityRole="summary">
            <Text style={[type.headline, { color: colors.ink }]}>{limit.title}</Text>
            <Text style={[type.callout, { color: colors.ink2, marginTop: 4 }]}>{limit.body}</Text>
            {limit.trial ? (
              <Text style={[type.callout, { color: colors.greenInk, fontWeight: '600', marginTop: 6 }]}>{limit.trial}</Text>
            ) : null}
          </View>
        ) : null}
        {onboarding && !view.subscribed ? (
          <>
            <Text style={[type.title1, { color: colors.ink, marginTop: 6 }]}>Fix your swing faster with Pro</Text>
            <View style={{ gap: 12, marginTop: 16 }}>
              {ONBOARDING_BENEFITS.map((b) => (
                <View key={b.text} style={{ flexDirection: 'row', alignItems: 'center', gap: 12 }}>
                  <View style={styles.benefitIcon}><SymbolView name={b.icon} size={17} tintColor={colors.gold} /></View>
                  <Text style={[type.callout, { color: colors.ink, flex: 1 }]}>{b.text}</Text>
                </View>
              ))}
            </View>
          </>
        ) : (
        <Text style={[type.title2, { color: colors.ink, marginTop: 6 }]}>
          {view.subscribed ? 'Your plan is active' : source === 'first_result' ? 'Keep improving your swing' : 'Swing Guru Pro'}
        </Text>
        )}
        {!view.subscribed && source === 'first_result' && fix ? (
          <Text style={[type.callout, { color: colors.ink, marginTop: 6 }]}>
            Your biggest fix: <Text style={{ fontWeight: '700', color: colors.gold }}>{fix}</Text>. Film again after practising and see it improve.
          </Text>
        ) : null}
        {onboarding && !view.subscribed ? null : (
        <Text style={[type.callout, { color: colors.ink2, marginTop: 6 }]}>
          {view.subscribed
            ? 'Thank you. Manage or cancel any time in the App Store.'
            : status?.paid_allowance === 'unlimited'
              ? 'Unlimited swing analyses, with the full breakdown, replay and drills.'
              : 'More swing analyses, with the full breakdown, replay and drills.'}
        </Text>
        )}
        {!view.subscribed && status?.enabled && !limit ? (
          <Text style={[type.footnote, { color: colors.ink3, marginTop: 6 }]}>
            Free includes {analysesCount(status.free_total)} each calendar month.
          </Text>
        ) : null}

        {loading ? (
          <View style={styles.loading}>
            <ActivityIndicator color={colors.green} />
            <Text style={[type.footnote, { color: colors.ink2, marginTop: 10 }]}>Loading the App Store…</Text>
          </View>
        ) : null}

        {!loading && !billingSupported && !view.subscribed ? (
          <Text style={[type.callout, { color: colors.ink2, marginTop: 18 }]}>
            In-app purchases are not available in this build. Your free analyses still work.
          </Text>
        ) : null}

        {!loading && unavailable && !view.subscribed ? (
          <View style={{ marginTop: 18 }}>
            <Text style={[type.callout, { color: colors.ink2 }]}>{unavailable}</Text>
            <View style={{ marginTop: 12, alignItems: 'flex-start' }}>
              <Button label="Try again" kind="secondary" small onPress={() => void checkAgain()} />
            </View>
          </View>
        ) : null}

        {!loading && view.showPlans ? offers.map((offer) => (
          <PlanRow key={offer.id} offer={offer} selected={offer.id === chosen} onPress={() => setChosen(offer.id)} />
        )) : null}

        {!loading && view.showPlans && !view.subscribed ? <TrialTimeline offer={selected} /> : null}

        {!loading && view.disclosure ? (
          <Text style={[type.footnote, { color: colors.ink2, marginTop: 14 }]}>{view.disclosure}</Text>
        ) : null}

        {notice ? (
          <View style={[styles.notice, notice.tone === 'error' && styles.noticeError]}>
            <Text style={[type.footnote, { color: notice.tone === 'error' ? colors.problemInk : colors.ink }]}>{notice.text}</Text>
            {notice.tone === 'info' ? (
              <View style={{ marginTop: 10, alignItems: 'flex-start' }}>
                <Button label="Check again" kind="secondary" small onPress={() => void checkAgain()} />
              </View>
            ) : null}
          </View>
        ) : null}
      </ScrollView>

      {!loading ? (
        <View style={{ gap: 10 }}>
          {view.subscribed ? (
            <>
              <Button block label="Continue" onPress={() => router.back()} />
              <Button label="Manage subscription" kind="secondary" small
                onPress={() => void WebBrowser.openBrowserAsync(manageUrl(status))} />
            </>
          ) : view.showBuy ? (
            <>
              <Button block busy={busy === 'purchase'} disabled={!selected || busy !== null} label={view.ctaLabel}
                onPress={() => void buy()} />
              {source === 'first_result' || onboarding ? (
                <Button label={onboarding ? 'Not now, use my free analysis' : 'Not now'} kind="quiet" block disabled={busy !== null}
                  onPress={dismiss} />
              ) : null}
              <Button label="Restore purchases" kind="quiet" small busy={busy === 'restore'} disabled={busy !== null}
                onPress={() => void restore()} />
            </>
          ) : (
            <>
              {/* No buyable plan right now: restoring must still be reachable. */}
              {onboarding ? <Button label="Continue with my free analysis" block disabled={busy !== null} onPress={dismiss} /> : null}
              <Button label="Restore purchases" kind="secondary" block busy={busy === 'restore'} disabled={busy !== null}
                onPress={() => void restore()} />
              <Button label="Try again" kind="quiet" small disabled={busy !== null} onPress={() => void checkAgain()} />
            </>
          )}
        </View>
      ) : null}

      <View style={styles.legal}>
        <Pressable accessibilityRole="link" onPress={() => void WebBrowser.openBrowserAsync(PRIVACY_URL)}>
          <Caption>Privacy policy</Caption>
        </Pressable>
        <Text style={[type.footnote, { color: colors.ink3 }]}>·</Text>
        <Pressable accessibilityRole="link" onPress={() => void WebBrowser.openBrowserAsync(TERMS_URL)}>
          <Caption>Terms of use</Caption>
        </Pressable>
        <Text style={[type.footnote, { color: colors.ink3 }]}>·</Text>
        <Pressable accessibilityRole="link" onPress={() => void WebBrowser.openBrowserAsync(manageUrl(status))}>
          <Caption>Manage in App Store</Caption>
        </Pressable>
      </View>
    </View>
  );
}

/** Today / before the last day / the first charge, for the selected plan when it has a free trial. */
function TrialTimeline({ offer }: { offer: PlanOffer | null }) {
  const steps = trialTimeline(offer);
  if (!steps) return null;
  return (
    <View style={styles.timeline} accessibilityLabel={steps.map((s) => `${s.when}: ${s.what}`).join('. ')}>
      {steps.map((s, i) => (
        <View key={s.when} style={{ flexDirection: 'row', gap: 12 }}>
          <View style={{ alignItems: 'center' }}>
            <View style={[styles.timelineDot, i === 0 && { backgroundColor: colors.gold }]} />
            {i < steps.length - 1 ? <View style={styles.timelineLine} /> : null}
          </View>
          <View style={{ flex: 1, paddingBottom: i < steps.length - 1 ? 14 : 0 }}>
            <Text style={[type.subhead, { color: colors.ink, fontWeight: '700' }]}>{s.when}</Text>
            <Text style={[type.footnote, { color: colors.ink2, marginTop: 2 }]}>{s.what}</Text>
          </View>
        </View>
      ))}
    </View>
  );
}

function PlanRow({ offer, selected, onPress }: { offer: PlanOffer; selected: boolean; onPress: () => void }) {
  // 1.0.8: for an eligible golfer the yearly card leads with "7 days free", the price right under it.
  const headline = offer.kind === 'yearly' ? trialHeadline(offer) : null;
  const priceLine = headline ? trialPriceLine(offer) : null;
  if (headline && priceLine) {
    return (
      <Pressable
        accessibilityRole="radio"
        accessibilityState={{ selected }}
        accessibilityLabel={`${offer.title}: ${headline}, ${priceLine}${offer.value ? `. ${offer.value}` : ''}`}
        onPress={onPress}
        style={({ pressed }) => [styles.plan, selected && styles.planOn, pressed && !selected && { backgroundColor: colors.surface2 }]}>
        <View style={styles.planText}>
          <View style={styles.planTitleRow}>
            <Text numberOfLines={1} style={[type.title2, { color: colors.ink, flexShrink: 1 }]}>{headline}</Text>
            {offer.badge ? (
              <View style={styles.badge}><Text style={{ color: colors.greenInk, fontSize: 12, fontWeight: '700' }}>{offer.badge}</Text></View>
            ) : null}
          </View>
          <Text style={[type.callout, { color: colors.ink, fontWeight: '600', marginTop: 4 }]}>{offer.title}, {priceLine}</Text>
          {offer.value ? (
            <Text style={[type.footnote, { color: colors.greenInk, fontWeight: '600', marginTop: 2 }]}>{offer.value}</Text>
          ) : null}
        </View>
        <SymbolView name={selected ? 'checkmark.circle.fill' : 'circle'} size={20} tintColor={selected ? colors.green : colors.lineStrong} />
      </Pressable>
    );
  }
  return (
    <Pressable
      accessibilityRole="radio"
      accessibilityState={{ selected }}
      accessibilityLabel={`${offer.title}, ${priceLabel(offer)}${offer.kind === 'yearly' ? ' per year' : offer.kind === 'monthly' ? ' per month' : ''}${offer.value ? `. ${offer.value}` : ''}`}
      onPress={onPress}
      style={({ pressed }) => [styles.plan, offer.kind === 'monthly' && styles.planQuiet, selected && styles.planOn,
        pressed && !selected && { backgroundColor: colors.surface2 }]}>
      <View style={styles.planText}>
        <View style={styles.planTitleRow}>
          <Text numberOfLines={2} style={[type.headline, { color: colors.ink, flexShrink: 1 }]}>{offer.title}</Text>
          {offer.badge ? (
            <View style={styles.badge}><Text style={{ color: colors.greenInk, fontSize: 12, fontWeight: '700' }}>{offer.badge}</Text></View>
          ) : null}
        </View>
        {offer.value ? (
          <Text style={[type.footnote, { color: offer.kind === 'monthly' ? colors.ink2 : colors.greenInk, fontWeight: offer.kind === 'monthly' ? '400' : '600', marginTop: 4 }]}>
            {offer.value}
          </Text>
        ) : null}
        <Text numberOfLines={2} style={[type.footnote, { color: colors.ink3, marginTop: 2 }]}>{planNote(offer)}</Text>
      </View>
      <View style={styles.planPrice}>
        <Text numberOfLines={1} style={[type.headline, { color: colors.ink }]}>{priceLabel(offer)}</Text>
        {offer.kind !== 'lifetime' ? (
          <Text style={[type.footnote, { color: colors.ink3, marginTop: 2 }]}>{offer.kind === 'yearly' ? 'per year' : 'per month'}</Text>
        ) : null}
      </View>
      <SymbolView name={selected ? 'checkmark.circle.fill' : 'circle'} size={20} tintColor={selected ? colors.green : colors.lineStrong} />
    </Pressable>
  );
}

const styles = themed(() => StyleSheet.create({
  screen: { flex: 1, backgroundColor: colors.bg, paddingHorizontal: space.gutter },
  benefitIcon: { width: 34, height: 34, borderRadius: radius.md, backgroundColor: colors.goldSoft, alignItems: 'center', justifyContent: 'center' },
  timeline: { marginTop: 16, padding: 16, borderRadius: radius.lg, backgroundColor: colors.surface, borderWidth: 1, borderColor: colors.line },
  timelineDot: { width: 12, height: 12, borderRadius: 6, backgroundColor: colors.green, marginTop: 4 },
  timelineLine: { width: 2, flex: 1, backgroundColor: colors.line, marginTop: 2 },
  header: { flexDirection: 'row', alignItems: 'center', justifyContent: 'space-between', paddingVertical: 10 },
  // 44 x 44 pt (Apple's minimum touch target), plus an 8 pt hitSlop.
  round: { width: 44, height: 44, borderRadius: 22, backgroundColor: colors.surface2, alignItems: 'center', justifyContent: 'center' },
  loading: { alignItems: 'center', paddingVertical: 34 },
  plan: { flexDirection: 'row', alignItems: 'center', gap: 12, padding: 16, marginTop: 12, borderRadius: radius.lg, borderWidth: 1, borderColor: colors.line, backgroundColor: colors.surface },
  planOn: { borderColor: colors.green, borderWidth: 2, padding: 15, backgroundColor: colors.greenTint },
  // Monthly stays available but quieter: smaller padding, no badge.
  planQuiet: { paddingVertical: 12 },
  // minWidth 0 + flexShrink keep a long title, badge and price on one row on narrow screens.
  planText: { flex: 1, minWidth: 0 },
  planTitleRow: { flexDirection: 'row', alignItems: 'center', gap: 8, flexWrap: 'wrap' },
  planPrice: { alignItems: 'flex-end', flexShrink: 0, maxWidth: '40%' },
  badge: { backgroundColor: colors.greenSoft, paddingHorizontal: 8, paddingVertical: 3, borderRadius: radius.pill, flexShrink: 0 },
  limit: { padding: 16, borderRadius: radius.lg, backgroundColor: colors.greenTint, borderWidth: 1, borderColor: colors.greenSoft, marginTop: 4, marginBottom: 14 },
  notice: { marginTop: 16, padding: 14, borderRadius: radius.md, backgroundColor: colors.surface2 },
  noticeError: { backgroundColor: colors.problemSoft },
  legal: { flexDirection: 'row', alignItems: 'center', justifyContent: 'center', gap: 8, paddingVertical: 12, flexWrap: 'wrap' },
}));
