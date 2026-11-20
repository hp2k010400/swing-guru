import { Stack } from 'expo-router';
import * as SplashScreen from 'expo-splash-screen';
import { StatusBar } from 'expo-status-bar';
import { useCallback, useEffect, useMemo, useRef, useSyncExternalStore } from 'react';
import { AppState } from 'react-native';

import { createAccountTeardown } from '@/lib/account-teardown';
import { flushAnalytics, trackAppOpened } from '@/lib/analytics';
import { forgetBillingIdentity } from '@/lib/billing';
import { useAccount, useAppleCredentialGuard } from '@/lib/identity';
import { startMetaSdk } from '@/lib/meta-ads';
import { cancelReminder, useReminderTaps } from '@/lib/reminders';
import { accessToken, runAuthTransition, sessionIdentity, signOutLocally } from '@/lib/session';
import { colors, subscribeTheme, theme } from '@/theme';

SplashScreen.preventAutoHideAsync();

export default function RootLayout() {
  const account = useAccount();
  const look = JSON.parse(useSyncExternalStore(subscribeTheme, () => JSON.stringify(theme()))) as ReturnType<typeof theme>;
  // Apple can revoke this app's credential (the golfer deleted their Apple ID, or stopped using it
  // with Swing Guru). Then this device must not keep using the session.
  //
  // The teardown is bound to the account that was checked and runs inside the transition queue,
  // re-checking after every await: a revocation event for account A that arrives once B is signed in
  // must never sign B out.
  const teardown = useMemo(() => createAccountTeardown({
    snapshot: sessionIdentity, serialise: runAuthTransition, forgetBillingIdentity, signOutLocally,
  }), []);
  const onAppleRevoked = useCallback(() => {
    void teardown.run(account.userId);
  }, [account.userId, teardown]);
  useAppleCredentialGuard(account, onAppleRevoked);
  // A tapped practice reminder opens Analyse.
  useReminderTaps();
  // A sign-out, account switch or deletion cancels the pending reminder (it was about the old account's
  // swing). Linking Apple keeps the same account id, so saving an analysis doesn't cancel it.
  const reminderOwner = useRef(account.userId);
  useEffect(() => {
    if (reminderOwner.current && account.userId !== reminderOwner.current) void cancelReminder();
    if (account.userId) reminderOwner.current = account.userId;
  }, [account.userId]);

  useEffect(() => {
    // Meta app-install attribution (iOS only, no IDFA, no prompt). Never blocks or breaks start-up.
    startMetaSdk();
    // app_opened (only if analytics is allowed): this cold start, then each return to the
    // foreground; queued events are sent as the app goes to the background.
    trackAppOpened('cold_start');
    let last = AppState.currentState;
    const sub = AppState.addEventListener('change', (next) => {
      // Only a real return from the background (not Control Centre / a system sheet, which is "inactive").
      if (next === 'active' && last === 'background') trackAppOpened('foreground');
      if (next === 'background') void flushAnalytics();
      last = next;
    });
    // Establish the (anonymous) session early so the first analysis is quick; never block the UI on it.
    accessToken().catch(() => undefined).finally(() => SplashScreen.hideAsync());
    return () => sub.remove();
  }, []);

  return (
    <>
      <StatusBar style={look.scheme === 'dark' ? 'light' : 'dark'} />
      {/* Remounting on the account id clears every screen's private state (queries, drafts, loaded
          details) on a sign-out, a switch or a deletion - not just the shared detail cache. The theme
          version is in the key too, so a theme change re-renders every screen with the new colours. */}
      <Stack key={`${account.userId ?? 'signed-out'}:${look.version}`}
             screenOptions={{ headerShown: false, contentStyle: { backgroundColor: colors.bg } }}>
        <Stack.Screen name="(tabs)" />
        <Stack.Screen name="analyze" options={{ presentation: 'formSheet', sheetAllowedDetents: [0.62, 0.9], sheetGrabberVisible: true }} />
        <Stack.Screen name="trim" options={{ presentation: 'fullScreenModal', gestureEnabled: false }} />
        <Stack.Screen name="club" />
        <Stack.Screen name="league" />
        <Stack.Screen name="paywall" options={{ presentation: 'fullScreenModal' }} />
        <Stack.Screen name="onboarding" options={{ presentation: 'fullScreenModal', gestureEnabled: false }} />
        <Stack.Screen name="record" options={{ presentation: 'fullScreenModal', gestureEnabled: false }} />
        <Stack.Screen name="feedback" options={{ presentation: 'modal' }} />
        <Stack.Screen name="processing/[id]" options={{ presentation: 'fullScreenModal', gestureEnabled: false }} />
        <Stack.Screen name="swing/[id]" />
        <Stack.Screen name="replay/[id]" options={{ presentation: 'fullScreenModal', contentStyle: { backgroundColor: colors.bg } }} />
      </Stack>
    </>
  );
}
