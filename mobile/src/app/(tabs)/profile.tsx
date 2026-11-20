// Profile: account, plan management, privacy and data controls (App Store requires in-app
// account deletion and a way to manage or restore a subscription).
import { router, useFocusEffect } from 'expo-router';
import * as AppleAuthentication from 'expo-apple-authentication';
import * as Application from 'expo-application';
import * as WebBrowser from 'expo-web-browser';
import { useCallback, useEffect, useRef, useState, useSyncExternalStore } from 'react';
import { SymbolView } from 'expo-symbols';
import { Alert, Linking, Pressable, Switch, Text, View } from 'react-native';

import { analyticsAvailable, consent, resetAnalyticsId, setConsent, subscribeConsent, track } from '@/lib/analytics';

import { MetaDiagnosticsCard } from '@/components/meta-diagnostics';
import { Button, Caption, Card, LargeTitle, Screen, Section } from '@/components/ui';
import { ApiError, api, friendly } from '@/lib/api';
import { deletionCopy, deletionOutcomeFrom } from '@/lib/account-core';
import { createAccountTeardown } from '@/lib/account-teardown';
import { billingSupported, forgetBillingIdentity, manageUrl, restorePlan, serverStatus, settlePurchase } from '@/lib/billing';
import { SIGN_IN_REQUIRED_COPY, freeLeftCopy, resetCopy } from '@/lib/billing-core';
import { appleAuthorizationForDeletion, appleSignInAvailable, signInWithApple, switchToAppleAccount, useAccount } from '@/lib/identity';
import { outcomeCopy } from '@/lib/identity-core';
import { versionLabel } from '@/lib/feedback-core';
import { afterSettleCopy } from '@/lib/paywall-view';
import { disableReminders, enableReminders, reminderChoice, reminderPermission } from '@/lib/reminders';
import { REMIND_AFTER_DAYS } from '@/lib/reminders-core';
import { devUserId, runAuthTransition, sessionIdentity, signOutLocally, supabase, userId } from '@/lib/session';
import { AUTH_MODE, IS_STAGING, PRIVACY_URL, SUPPORT_URL, TERMS_URL } from '@/config';
import type { BillingStatus } from '@/lib/types';
import { colors, radius, setThemeMode, theme, type, type ThemeMode } from '@/theme';

/**
 * The plain-language plan card. The free allowance is three analyses per calendar month and paid
 * plans are unlimited, so Pro never shows a quota; a missing durable account is stated honestly and
 * is never dressed up as a purchase.
 */
function planSummary(plan: BillingStatus): { caption: string; body: string } {
  if (plan.access === 'pro') {
    return {
      caption: 'Swing Guru Pro',
      body: plan.expires_at
        ? `Active until ${new Date(plan.expires_at).toLocaleDateString()}`
        : 'Lifetime access - thank you.',
    };
  }
  if (plan.access === 'sign_in_required') {
    return { caption: 'Free analyses', body: SIGN_IN_REQUIRED_COPY };
  }
  const reset = resetCopy(plan);
  if (plan.access === 'in_progress') {
    return {
      caption: 'Free analyses this month',
      body: `Your earlier free analyses are still finishing. ${reset ?? 'Your remaining free analyses will be available again shortly.'}`,
    };
  }
  if (plan.access === 'exhausted') {
    return {
      caption: 'Free analyses this month',
      body: [plan.free_total === 1 ? "You've used your free analysis this month." : `You've used all ${plan.free_total} free analyses this month.`,
        reset, 'Choose a plan to keep analysing.']
        .filter(Boolean).join(' '),
    };
  }
  const left = freeLeftCopy(plan) ?? `${plan.free_remaining} free analyses left this month`;
  return {
    caption: 'Free analyses this month',
    body: [`${left}.`, reset, 'After that, choose a plan to keep analysing.'].filter(Boolean).join(' '),
  };
}

const MODES: { mode: ThemeMode; label: string; icon: 'iphone' | 'sun.max.fill' | 'moon.fill' }[] = [
  { mode: 'system', label: 'Match iPhone', icon: 'iphone' },
  { mode: 'light', label: 'Light', icon: 'sun.max.fill' },
  { mode: 'dark', label: 'Dark', icon: 'moon.fill' },
];

/** Light / Dark / Match iPhone. Changing it re-renders the app, so come straight back to Profile. */
/** Practice reminders on/off. Turning on asks iOS the first time; if iOS has them off, it opens Settings. */
function ReminderSetting() {
  const [on, setOn] = useState(false);
  const [blocked, setBlocked] = useState(false);
  const refresh = useCallback(() => {
    void reminderPermission().then((p) => {
      setBlocked(p === 'denied');
      setOn(p === 'granted' && reminderChoice() === 'on');
    });
  }, []);
  useFocusEffect(refresh);

  async function toggle(next: boolean) {
    if (!next) {
      await disableReminders('profile');
      setOn(false);
      return;
    }
    if (blocked) {
      void Linking.openSettings();
      return;
    }
    setOn(await enableReminders('profile'));
    refresh();
  }

  return (
    <Card tone="flat">
      <View style={{ flexDirection: 'row', alignItems: 'center', gap: 12 }}>
        <Text style={[type.callout, { color: colors.ink, flex: 1 }]}>Practice reminders</Text>
        <Switch value={on} onValueChange={(v) => void toggle(v)}
          trackColor={{ true: colors.green, false: colors.surface3 }} accessibilityLabel="Practice reminders" />
      </View>
      <Text style={[type.footnote, { color: colors.ink2, marginTop: 8 }]}>
        {blocked
          ? 'Notifications are off for Swing Guru. Turn them on in Settings to get reminders.'
          : `One reminder ${REMIND_AFTER_DAYS} days after your latest analysis to film again and check your progress.`}
      </Text>
    </Card>
  );
}

function AppearancePicker() {
  const current = theme().mode;
  return (
    <View style={{ flexDirection: 'row', gap: 8 }}>
      {MODES.map(({ mode, label, icon }) => {
        const on = mode === current;
        return (
          <Pressable key={mode} accessibilityRole="radio" accessibilityState={{ selected: on }} accessibilityLabel={label}
            onPress={() => {
              if (on) return;
              setThemeMode(mode);
              track('theme_changed', { mode });
              setTimeout(() => router.navigate('/profile'), 0);
            }}
            style={({ pressed }) => [{
              flex: 1, alignItems: 'center', gap: 6, paddingVertical: 14, borderRadius: radius.md, borderWidth: 1.5,
              borderColor: on ? colors.green : colors.line, backgroundColor: on ? colors.greenSoft : colors.surface,
            }, pressed && { opacity: 0.8 }]}>
            <SymbolView name={icon} size={20} tintColor={on ? colors.greenInk : colors.ink2} />
            <Text style={[type.footnote, { color: on ? colors.greenInk : colors.ink2, fontWeight: '600' }]}>{label}</Text>
          </Pressable>
        );
      })}
    </View>
  );
}

// Module-level: every dependency is a module singleton, so one teardown serves the screen.
const teardown = createAccountTeardown({
  snapshot: sessionIdentity, serialise: runAuthTransition, forgetBillingIdentity, signOutLocally,
});

export default function Profile() {
  const [busy, setBusy] = useState(false);
  const [showMeta, setShowMeta] = useState(false);
  const [plan, setPlan] = useState<BillingStatus | null>(null);
  const [planError, setPlanError] = useState<string | null>(null);
  const [restoring, setRestoring] = useState(false);
  const account = useAccount();
  const sharing = useSyncExternalStore(subscribeConsent, consent);
  const [appleReady, setAppleReady] = useState(false);
  const [signingIn, setSigningIn] = useState(false);
  // Set when the Apple sign-in itself succeeded but the server could not store the grant yet: the
  // golfer then gets one small, explicit "finish setup" action in the Apple card.
  const [setupPending, setSetupPending] = useState(false);
  useEffect(() => {
    let alive = true;
    appleSignInAvailable().then((ok) => alive && setAppleReady(ok));
    return () => {
      alive = false;
    };
  }, []);

  const loadPlan = useCallback(async () => {
    try {
      setPlan(await serverStatus());
      setPlanError(null);
    } catch (error) {
      // Never leave a previous account's plan on screen.
      setPlan(null);
      setPlanError(
        error instanceof Error && error.message.includes('identity')
          ? 'Please sign in again to see your plan.'
          : friendly(error),
      );
    }
  }, []);
  useFocusEffect(
    useCallback(() => {
      void loadPlan();
    }, [loadPlan]),
  );

  const restore = async () => {
    setRestoring(true);
    try {
      const who = await userId();
      const result = await restorePlan();
      if (result.outcome === 'cancelled') return;
      if (result.outcome === 'failed' || result.outcome === 'unavailable') {
        Alert.alert("Couldn't restore purchases", result.message ?? 'Please try again.');
        return;
      }
      if (result.outcome === 'identity_changed') {
        Alert.alert('Your account changed', result.message ?? 'Please sign in again and restore.');
        return;
      }
      if (result.outcome === 'pending') {
        Alert.alert('Almost there', result.message ?? 'The App Store is still confirming your purchase. Try again in a moment.');
        return;
      }
      const { state, status: server } = await settlePurchase(who, result.entitled);
      if (state === 'identity_changed') {
        setPlan(null);
        const copy = afterSettleCopy(state, result.entitled, 'restore');
        Alert.alert(copy?.title ?? 'Your account changed', copy?.body ?? 'Please sign in again and restore.');
        return;
      }
      if (server) setPlan(server);
      const copy = afterSettleCopy(state, result.entitled, 'restore');
      if (copy) Alert.alert(copy.title, copy.body);
    } catch (error) {
      Alert.alert("Couldn't restore purchases", friendly(error));
    } finally {
      setRestoring(false);
    }
  };

  // A ref, not state: two taps in the same frame both read the old state and would open two sheets.
  const appleLock = useRef(false);
  const appleSignIn = async () => {
    if (appleLock.current) return;
    appleLock.current = true;
    setSigningIn(true);
    let handedOff = false;
    try {
      const { outcome, pending } = await signInWithApple();
      if (outcome === 'existing_account' && pending) {
        const copy = outcomeCopy(outcome)!;
        handedOff = true; // the lock stays held until the golfer answers the switch question
        Alert.alert(copy.title, copy.body, [
          { text: 'Cancel', style: 'cancel', onPress: () => { appleLock.current = false; setSigningIn(false); } },
          {
            text: 'Switch account',
            onPress: async () => {
              try {
                const switched = await switchToAppleAccount(pending);
                setSetupPending(switched === 'link_incomplete');
                const done = outcomeCopy(switched);
                if (done) Alert.alert(done.title, done.body);
              } catch (error) {
                Alert.alert("Couldn't switch account", friendly(error));
              } finally {
                appleLock.current = false;
                setSigningIn(false);
                void loadPlan();
              }
            },
          },
        ], { cancelable: false });
        return;
      }
      const copy = outcomeCopy(outcome);
      if (copy) Alert.alert(copy.title, copy.body);
      if (outcome === 'linked' || outcome === 'signed_in') track('signup_completed', { method: 'apple' });
      setSetupPending(outcome === 'link_incomplete');
      void loadPlan();
    } catch (error) {
      Alert.alert("Couldn't sign in with Apple", friendly(error));
    } finally {
      if (!handedOff) {
        appleLock.current = false;
        setSigningIn(false);
      }
    }
  };

  const signOut = () =>
    Alert.alert('Sign out?', 'Your swings stay in your account. Sign in with Apple again to see them.', [
      { text: 'Cancel', style: 'cancel' },
      {
        text: 'Sign out', style: 'destructive',
        // Serialised with every other auth transition, and bound to the account that asked.
        onPress: async () => {
          if (await teardown.run(account.userId)) {
            resetAnalyticsId();
            setPlan(null);
            router.replace('/');
          }
        },
      },
    ]);

  /**
   * Local teardown after the server confirms deletion, bound to the account that asked for it: a
   * deletion that completes while a different golfer is signed in must not sign *them* out. The
   * server records any confirmed Apple revocation; this clears the local session, caches and
   * RevenueCat identity and deliberately does not reopen the Apple sheet.
   */
  const finishDeletion = async (expectedUserId: string | null) => {
    if (!(await teardown.run(expectedUserId))) return;
    resetAnalyticsId();
    setPlan(null);
    router.replace('/');
  };

  /**
   * mode `plain` deletes a guest/already-enrolled account; `fresh_apple` first proves the Apple
   * account (for one linked before the server stored a refresh token); `manual` is Apple's TN3194
   * fallback when no credential can be obtained - the data is deleted and the golfer is pointed at
   * iOS Settings to stop using Sign in with Apple, and the server never claims it revoked anything.
   */
  const runDeleteAccount = async (mode: 'plain' | 'fresh_apple' | 'manual') => {
    setBusy(true);
    const expectedUserId = account.userId;
    try {
      let body: { authorization_code?: string; nonce?: string; manual_apple_revocation?: boolean } | undefined;
      if (mode === 'fresh_apple') {
        // Snapshot the account before the Apple sheet: the deletion belongs to whoever asked.
        const intent = sessionIdentity();
        const fresh = await appleAuthorizationForDeletion();
        if (intent.owner !== null && sessionIdentity().owner !== intent.owner) {
          const copy = deletionCopy('retry');
          if (copy) Alert.alert(copy.title, copy.body);
          return;
        }
        if (!fresh) {
          Alert.alert(
            "Couldn't sign in with Apple",
            'You can still delete your account and your data. You would then need to stop using Sign in with Apple for Swing Guru in iOS Settings.',
            [
              { text: 'Try again', onPress: () => void runDeleteAccount('fresh_apple') },
              { text: 'Delete anyway', style: 'destructive', onPress: () => void runDeleteAccount('manual') },
            ],
          );
          return;
        }
        body = fresh;
      } else if (mode === 'manual') {
        body = { manual_apple_revocation: true };
      }
      const result = await api.deleteAccount(body);
      await finishDeletion(expectedUserId);
      const copy = deletionCopy(deletionOutcomeFrom(result, null));
      if (copy) Alert.alert(copy.title, copy.body);
    } catch (e) {
      const error = e instanceof ApiError ? { status: e.status, code: e.code } : null;
      const outcome = deletionOutcomeFrom(null, error);
      if (outcome === 'already_deleted') {
        await finishDeletion(expectedUserId);
        return;
      }
      if (outcome === 'reauthorize') {
        const copy = deletionCopy('reauthorize')!;
        Alert.alert(copy.title, copy.body, [
          { text: 'Not now', style: 'cancel' },
          { text: 'Continue', onPress: () => void runDeleteAccount('fresh_apple') },
        ]);
        return;
      }
      const copy = deletionCopy(outcome);
      if (copy) Alert.alert(copy.title, copy.body);
    } finally {
      setBusy(false);
    }
  };

  const deleteAccount = () =>
    Alert.alert('Delete your account?', 'All your swings, videos and analyses will be permanently deleted. This cannot be undone.', [
      { text: 'Cancel', style: 'cancel' },
      { text: 'Delete everything', style: 'destructive', onPress: () => void runDeleteAccount('plain') },
    ]);

  return (
    <Screen>
      <LargeTitle>Profile</LargeTitle>
      <Card>
        <Caption>Account</Caption>
        {AUTH_MODE === 'dev' ? (
          <>
            <Text style={[type.callout, { color: colors.ink, marginTop: 8 }]}>Test account on this device.</Text>
            <Text selectable style={[type.footnote, { color: colors.ink2, marginTop: 4, fontVariant: ['tabular-nums'] }]}>
              Test account ID: {devUserId()}
            </Text>
          </>
        ) : account.provider === 'apple' ? (
          <>
            <Text style={[type.callout, { color: colors.ink, marginTop: 8 }]}>Signed in with Apple</Text>
            {account.email ? <Text style={[type.footnote, { color: colors.ink2, marginTop: 4 }]}>{account.email}</Text> : null}
            <Text style={[type.footnote, { color: colors.ink2, marginTop: 6 }]}>
              Your swings and free analyses stay with your Apple ID if you reinstall or change phone.
            </Text>
            {setupPending ? (
              <Text style={[type.footnote, { color: colors.ink2, marginTop: 6 }]}>
                One step left: finish setting up account deletion for this Apple ID.
              </Text>
            ) : null}
            <View style={{ flexDirection: 'row', gap: 8, marginTop: 12, flexWrap: 'wrap' }}>
              {setupPending ? (
                <Button label="Finish Apple setup" kind="secondary" small busy={signingIn}
                  onPress={() => { if (!signingIn) void appleSignIn(); }} />
              ) : null}
              <Button label="Sign out" kind="quiet" small onPress={signOut} />
            </View>
          </>
        ) : supabase ? (
          <>
            <Text style={[type.callout, { color: colors.ink, marginTop: 8 }]}>
              You&apos;re using Swing Guru as a guest. Your swings are saved to a private account on this iPhone.
            </Text>
            {appleReady ? (
              <>
                <AppleAuthentication.AppleAuthenticationButton
                  buttonType={AppleAuthentication.AppleAuthenticationButtonType.SIGN_IN}
                  buttonStyle={AppleAuthentication.AppleAuthenticationButtonStyle.BLACK}
                  cornerRadius={12}
                  style={{ height: 46, marginTop: 14, opacity: signingIn ? 0.6 : 1 }}
                  onPress={() => { if (!signingIn) void appleSignIn(); }}
                />
                <Text style={[type.footnote, { color: colors.ink2, marginTop: 8 }]}>
                  Keeps your swings and free analyses if you reinstall the app or change phone. Your swings so far come with you.
                </Text>
              </>
            ) : null}
          </>
        ) : null}
      </Card>
      {plan?.enabled ? (
        <Section title="Plan">
          <Card tone={plan.access === 'pro' ? 'tint' : 'flat'}>
            <Caption>{planSummary(plan).caption}</Caption>
            <Text style={[type.callout, { color: colors.ink, marginTop: 8 }]}>
              {planSummary(plan).body}
            </Text>
            {planError ? <Text style={[type.footnote, { color: colors.ink2, marginTop: 6 }]}>{planError}</Text> : null}
            {!billingSupported ? (
              <Text style={[type.footnote, { color: colors.ink2, marginTop: 6 }]}>
                In-app purchases are not available in this build.
              </Text>
            ) : null}
            <View style={{ flexDirection: 'row', gap: 8, marginTop: 12, flexWrap: 'wrap' }}>
              {plan.access === 'pro' ? (
                <Button label="Manage subscription" kind="secondary" small
                  onPress={() => void WebBrowser.openBrowserAsync(manageUrl(plan))} />
              ) : (
                <Button label="See plans" small disabled={!billingSupported} onPress={() => router.push({ pathname: '/paywall', params: { from: 'profile' } })} />
              )}
              <Button label="Restore purchases" kind="quiet" small busy={restoring}
                disabled={!billingSupported} onPress={() => void restore()} />
            </View>
          </Card>
        </Section>
      ) : null}
      <Section title="Progress">
        <Button label="Your rank and progress" icon="chart.line.uptrend.xyaxis" kind="secondary" block onPress={() => router.push('/progress')} />
      </Section>
      <Section title="Reminders">
        <ReminderSetting />
      </Section>
      <Section title="Appearance">
        <AppearancePicker />
      </Section>
      {analyticsAvailable ? (
        <Section title="Usage data">
          <Card tone="flat">
            <View style={{ flexDirection: 'row', alignItems: 'center', gap: 12 }}>
              <Text style={[type.callout, { color: colors.ink, flex: 1 }]}>Share anonymous usage data</Text>
              <Switch value={sharing === 'granted'} onValueChange={(on) => setConsent(on ? 'granted' : 'denied')}
                trackColor={{ true: colors.green, false: colors.surface3 }} accessibilityLabel="Share anonymous usage data" />
            </View>
            <Text style={[type.footnote, { color: colors.ink2, marginTop: 8 }]}>
              Helps us see which features are useful. Never includes your videos, measurements, name or email. Stored in the EU.
            </Text>
          </Card>
        </Section>
      ) : null}
      <Section title="Privacy">
        <Card tone="flat">
          <Text style={[type.callout, { color: colors.ink }]}>
            Your swing videos are private to you. We delete the original upload once it has been analysed, and keep the replay and its
            analysis until you delete the swing. We never use your videos to train models.
          </Text>
          <View style={{ flexDirection: 'row', gap: 8, marginTop: 12, flexWrap: 'wrap' }}>
            <Button label="Privacy policy" kind="secondary" small onPress={() => WebBrowser.openBrowserAsync(PRIVACY_URL)} />
            <Button label="Terms of use" kind="secondary" small onPress={() => WebBrowser.openBrowserAsync(TERMS_URL)} />
            <Button label="Support" kind="secondary" small onPress={() => WebBrowser.openBrowserAsync(SUPPORT_URL)} />
            <Button label="Send feedback" kind="secondary" small onPress={() => router.push('/feedback')} />
          </View>
        </Card>
      </Section>
      <Section title="Your data">
        <Button label="Delete account and all swings" kind="danger" busy={busy} onPress={deleteAccount} />
      </Section>
      {/* Which binary is installed (version and build number), so support and testing never have to guess. */}
      {/* Long-press opens the hidden Meta SDK diagnostics used for attribution testing. */}
      <Text onLongPress={() => setShowMeta((v) => !v)} suppressHighlighting
        style={[type.footnote, { color: colors.ink3, textAlign: 'center', marginTop: 24 }]}>
        Swing Guru {versionLabel(Application.nativeApplicationVersion, Application.nativeBuildVersion) ?? ''}
        {IS_STAGING ? ' · TEST SERVER (test data only)' : ''}
      </Text>
      {showMeta ? <MetaDiagnosticsCard /> : null}
    </Screen>
  );
}
