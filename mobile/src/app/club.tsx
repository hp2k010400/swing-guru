// Club: which club was this swing? Chosen before the upload so the swing is stored with it.
// Also opened from the Summary (?swing=<id>) to change a swing's club.
import * as AppleAuthentication from 'expo-apple-authentication';
import { router, useLocalSearchParams } from 'expo-router';
import { SymbolView } from 'expo-symbols';
import { useEffect, useRef, useState } from 'react';
import { Alert, Pressable, ScrollView, StyleSheet, Text, View } from 'react-native';

import { Button, Caption, SafeArea } from '@/components/ui';
import { track } from '@/lib/analytics';
import { api, ApiError, friendly } from '@/lib/api';
import { appleSignInAvailable, signInWithApple, switchToAppleAccount } from '@/lib/identity';
import { outcomeCopy } from '@/lib/identity-core';
import { checkTrimmed, submit } from '@/lib/analysis';
import { SIGN_IN_REQUIRED_COPY } from '@/lib/billing-core';
import { clubChanged } from '@/lib/club-events';
import { draft } from '@/lib/draft';
import { haptic } from '@/lib/haptics';
import { errorPrompt } from '@/lib/job-start';
import { CLUB_GROUPS, type Club } from '@/lib/types';
import { colors, radius, space, theme, type, themed } from '@/theme';

type Choice = Club | 'unsure';
const LAST = 'sg.lastClub';

function lastClub(): Choice | null {
  try {
    return (localStorage.getItem(LAST) as Choice | null) ?? null;
  } catch {
    return null;
  }
}

export default function ClubScreen() {
  const { swing, current } = useLocalSearchParams<{ swing?: string; current?: string }>();
  const editing = Boolean(swing);
  const [choice, setChoice] = useState<Choice | null>(() => (editing ? ((current as Club) ?? 'unsure') : lastClub()));
  const [busy, setBusy] = useState(false);
  // Set when the server answers 402: the trimmed draft is kept so a purchase can come straight back.
  const [needsPlan, setNeedsPlan] = useState(false);
  // Set when the server answers 403: free analyses need a durable account. Not a purchase prompt.
  const [needsAccount, setNeedsAccount] = useState(false);
  const [appleReady, setAppleReady] = useState(false);
  useEffect(() => {
    let alive = true;
    appleSignInAvailable().then((ok) => alive && setAppleReady(ok)).catch(() => undefined);
    return () => { alive = false; };
  }, []);

  const back = () => (router.canGoBack() ? router.back() : router.replace('/'));

  // A ref, not state: two taps in the same frame would otherwise open two Apple sheets.
  const signingIn = useRef(false);
  async function signInAndContinue() {
    if (signingIn.current) return;
    signingIn.current = true;
    try {
      const { outcome, pending } = await signInWithApple();
      if (outcome === 'linked' || outcome === 'signed_in') {
        track('signup_completed', { method: 'apple', from: 'analyse' });
        setNeedsAccount(false);
        await done(); // same draft, same club: the analysis starts now
        return;
      }
      if (outcome === 'existing_account' && pending) {
        const copy = outcomeCopy(outcome)!;
        Alert.alert(copy.title, copy.body, [
          { text: 'Cancel', style: 'cancel' },
          {
            text: 'Switch account',
            onPress: async () => {
              try {
                const switched = await switchToAppleAccount(pending);
                const after = outcomeCopy(switched);
                if (after) Alert.alert(after.title, `${after.body} Choose your swing video again to analyse it.`);
              } catch (error) {
                Alert.alert("Couldn't switch account", friendly(error));
              }
            },
          },
        ]);
        return;
      }
      const copy = outcomeCopy(outcome);
      if (copy) Alert.alert(copy.title, copy.body);
    } catch (error) {
      Alert.alert("Couldn't sign in with Apple", friendly(error));
    } finally {
      signingIn.current = false;
    }
  }

  async function done() {
    if (!choice) return;
    const club = choice === 'unsure' ? null : choice;
    setBusy(true);
    try {
      if (editing && swing) {
        await api.setClub(swing, club);
        clubChanged(swing, club);
        return back();
      }
      const d = draft.get();
      if (!d) return router.replace('/analyze');
      let video;
      try {
        video = await (d.trimmed ?? Promise.resolve(d.video));
      } catch {
        Alert.alert("Couldn't prepare this video", 'Try choosing it again, or pick a slightly different selection.');
        return;
      }
      const problem = checkTrimmed(video);
      if (problem) {
        Alert.alert("This selection can't be analysed", problem);
        return;
      }
      try {
        localStorage.setItem(LAST, choice);
      } catch {
        /* a convenience only */
      }
      const id = await submit(video, club);
      draft.clear();
      router.dismissAll();
      router.push({ pathname: '/processing/[id]', params: { id } });
    } catch (error) {
      // Only the server's documented codes ask for a prompt: a generic failure (including an
      // ownership/proxy 403) must stay an honest error, never a fabricated sign-in or paywall.
      const prompt = errorPrompt(error instanceof ApiError ? { code: error.code } : null);
      if (prompt === 'plan') {
        // The draft (video, trim, club) stays in memory and the club choice is untouched, so
        // "Analyse swing" can be tapped again after the purchase - no second upload or job.
        setNeedsPlan(true);
        router.push({ pathname: '/paywall', params: { from: 'limit' } });
        return;
      }
      if (prompt === 'account') {
        // The draft stays in memory too, and the golfer is told the honest reason without being sent
        // to the paywall: the missing thing is an account, not a purchase. Offer Sign in with Apple
        // straight away; after it, the analysis starts by itself.
        setNeedsAccount(true);
        if (await appleSignInAvailable()) {
          Alert.alert('Sign in to analyse your swing',
            'Your free analysis needs Sign in with Apple, so it stays with you if you reinstall the app or change phone. Your video is still here.',
            [{ text: 'Not now', style: 'cancel' }, { text: 'Sign in with Apple', onPress: () => void signInAndContinue() }]);
        }
        return;
      }
      Alert.alert(editing ? "Couldn't save the club" : "Couldn't start the analysis", friendly(error));
    } finally {
      setBusy(false);
    }
  }

  return (
    <SafeArea style={styles.screen} edges={['top', 'bottom']}>
      <View style={styles.header}>
        <Pressable accessibilityRole="button" accessibilityLabel="Back" onPress={back} hitSlop={8} style={styles.round}>
          <SymbolView name={editing ? 'xmark' : 'chevron.left'} size={16} tintColor={colors.ink} />
        </Pressable>
        <Text style={[type.headline, { color: colors.ink }]}>{editing ? 'Change club' : 'Which club?'}</Text>
        <View style={{ width: 44 }} />
      </View>

      <ScrollView contentContainerStyle={{ paddingBottom: 24 }} showsVerticalScrollIndicator={false}>
        <Text style={[type.callout, { color: colors.ink2, marginTop: 4 }]}>
          Saved with this swing so you can compare like with like.
        </Text>
        {CLUB_GROUPS.map((group) => (
          <View key={group.label} style={{ marginTop: 22 }}>
            <Caption style={{ marginBottom: 10 }}>{group.label}</Caption>
            <View style={styles.grid}>
              {group.clubs.map((club) => (
                <Option key={club} label={club} selected={choice === club} onPress={() => setChoice(club)} />
              ))}
            </View>
          </View>
        ))}
        <View style={[styles.grid, { marginTop: 22 }]}>
          <Option label="Other" selected={choice === 'Other'} onPress={() => setChoice('Other')} />
          <Option label="I'm not sure" selected={choice === 'unsure'} onPress={() => setChoice('unsure')} />
        </View>
      </ScrollView>

      {needsPlan && !editing ? (
        <Text style={[type.footnote, { color: colors.ink2, textAlign: 'center', marginBottom: 10 }]}>
          You&apos;ve reached this month&apos;s free limit. Your video is still here: upgrade to Swing Guru Pro, then tap Analyse swing again.
        </Text>
      ) : null}
      {needsAccount && !editing ? (
        <View style={{ marginBottom: 12 }}>
          <Text style={[type.footnote, { color: colors.ink2, textAlign: 'center', marginBottom: 10 }]}>
            {SIGN_IN_REQUIRED_COPY} Your video is still here.
          </Text>
          {appleReady ? (
            <AppleAuthentication.AppleAuthenticationButton
              buttonType={AppleAuthentication.AppleAuthenticationButtonType.CONTINUE}
              buttonStyle={theme().scheme === 'dark' ? AppleAuthentication.AppleAuthenticationButtonStyle.WHITE : AppleAuthentication.AppleAuthenticationButtonStyle.BLACK}
              cornerRadius={12}
              style={{ height: 46 }}
              onPress={() => void signInAndContinue()}
            />
          ) : null}
        </View>
      ) : null}
      <Button label={editing ? 'Save' : 'Analyse swing'} icon={editing ? undefined : 'sparkles'} block busy={busy} disabled={!choice} onPress={done} />
    </SafeArea>
  );
}

function Option({ label, selected, onPress }: { label: string; selected: boolean; onPress: () => void }) {
  return (
    <Pressable accessibilityRole="radio" accessibilityState={{ selected }} accessibilityLabel={label} onPress={() => { haptic('selection'); onPress(); }}
      style={({ pressed }) => [styles.option, selected && styles.optionOn, pressed && !selected && { backgroundColor: colors.surface2 }]}>
      <Text style={{ color: selected ? '#fff' : colors.ink, fontSize: 15, fontWeight: '600' }} numberOfLines={1}>{label}</Text>
    </Pressable>
  );
}

const styles = themed(() => StyleSheet.create({
  screen: { flex: 1, backgroundColor: colors.bg, paddingHorizontal: space.gutter },
  header: { flexDirection: 'row', alignItems: 'center', justifyContent: 'space-between', paddingVertical: 10 },
  round: { width: 44, height: 44, borderRadius: 22, backgroundColor: colors.surface2, alignItems: 'center', justifyContent: 'center' },
  grid: { flexDirection: 'row', flexWrap: 'wrap', gap: 8 },
  option: { flexGrow: 1, flexBasis: '30%', height: 46, paddingHorizontal: 10, borderRadius: radius.md, borderWidth: 1, borderColor: colors.lineStrong, backgroundColor: colors.surface, alignItems: 'center', justifyContent: 'center' },
  optionOn: { backgroundColor: colors.green, borderColor: colors.green },
}));
