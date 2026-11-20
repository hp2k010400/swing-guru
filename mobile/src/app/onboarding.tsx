// First-launch onboarding (once, skippable): what you get, how to film, the Pro trial offer (1.0.7, its own
// screen: app/paywall.tsx with from=onboarding), then a real example analysis / film / upload.
import { router } from 'expo-router';
import { SymbolView, type SFSymbol } from 'expo-symbols';
import { useEffect, useRef, useState } from 'react';
import { Pressable, StyleSheet, Text, View } from 'react-native';

import { AnalyticsConsentCard, useConsentPending } from '@/components/analytics-consent';
import { FilmingIllustration } from '@/components/filming-illustration';
import { Button, SafeArea } from '@/components/ui';
import { track } from '@/lib/analytics';
import { api } from '@/lib/api';
import { billingSupported, serverStatus } from '@/lib/billing';
import { onboardingPaywallSkipReason } from '@/lib/first-result';
import { trialRecordedHere } from '@/lib/trial-flag';
import { openExample } from '@/lib/example';
import { FILMING_STEPS } from '@/lib/filming-tips';
import { markOnboardingSeen } from '@/lib/onboarding';
import { colors, radius, space, type, themed } from '@/theme';

const STEPS = ['rated', 'film', 'example'] as const;
/** Longest Continue on "How to film" waits for the trial check before moving on without the offer. */
const PRO_CHECK_WAIT_MS = 5000;

export default function Onboarding() {
  const [step, setStep] = useState(0);
  const [exampleReady, setExampleReady] = useState<boolean | null>(null);
  // 1.0.7: the trial offer comes just before the last screen, when purchases work and the golfer has no
  // Pro and no trial yet. Checked in the background from launch (server plan + App Store prices and trial
  // eligibility, which can take a few seconds); Continue on "How to film" waits for it up to
  // PRO_CHECK_WAIT_MS, then carries on without the offer.
  // null = unknown (the check failed or is still running): the offer is shown anyway, because a first
  // launch has no Pro yet and the paywall handles unavailable prices itself. Only a definite answer
  // (Pro, trial started, billing off) skips it.
  const offerPro = useRef<boolean | null>(null);
  const proCheck = useRef<Promise<boolean | null>>(Promise.resolve(false));
  const [checking, setChecking] = useState(false);
  const offered = useRef(false);                    // shown at most once per onboarding
  const skipReason = useRef<string | null>(null);   // analytics only: why the offer was not shown
  const viewed = useRef(false);
  const askConsent = useConsentPending();           // 1.0.8: the analytics question on the first screen
  useEffect(() => {
    if (!viewed.current) {
      viewed.current = true;
      track('onboarding_viewed', {});
    }
    // The last screen offers the example only when the server has one.
    api.example().then(() => setExampleReady(true)).catch(() => setExampleReady(false));
    if (billingSupported) {
      // Only the server's plan is needed to decide (fast). The paywall itself loads the App Store prices
      // and shows the free trial + timeline when this Apple ID is eligible, plain plans otherwise.
      proCheck.current = (async () => {
        const s = await serverStatus();
        const reason = onboardingPaywallSkipReason({
          supported: billingSupported, enabled: s.enabled, access: s.access, trialStarted: trialRecordedHere(),
        });
        offerPro.current = reason === null;
        if (reason) skipReason.current = reason;
        return offerPro.current;
      })().catch(() => null);
    } else {
      skipReason.current = 'billing_off';
    }
  }, []);

  const finish = (to: 'example' | 'film' | 'upload' | 'home') => {
    markOnboardingSeen('completed');
    track('onboarding_completed', { next: to });
    if (to === 'example') {
      openExample({ replace: true });
    } else if (to === 'film') {
      router.replace('/record');
    } else if (to === 'upload') {
      router.replace('/analyze');
    } else {
      router.back();
    }
  };
  const skip = () => {
    markOnboardingSeen('skipped');
    track('onboarding_skipped', { step: STEPS[step] });
    router.back();
  };
  const next = async () => {
    if (checking) return;
    const to = Math.min(step + 1, STEPS.length - 1);
    // Arriving at the last screen: show the trial offer over it first. "Not now" (or a purchase) closes the
    // offer and leaves the golfer on See the example / Film my swing / Upload.
    if (STEPS[to] === 'example' && billingSupported) {
      setChecking(true);
      const offer = await Promise.race([
        proCheck.current,
        new Promise<null>((resolve) => setTimeout(() => resolve(null), PRO_CHECK_WAIT_MS)),
      ]);
      setChecking(false);
      setStep(to);
      if (offer !== false && !offered.current) {
        offered.current = true;
        router.push({ pathname: '/paywall', params: { from: 'onboarding' } });
      } else if (offer === false) {
        track('onboarding_paywall_skipped', { reason: skipReason.current ?? 'unknown' });
      }
      return;
    }
    if (STEPS[to] === 'example') track('onboarding_paywall_skipped', { reason: skipReason.current ?? 'billing_off' });
    setStep(to);
  };

  return (
    <SafeArea style={styles.screen} edges={['top', 'bottom']}>
      <View style={styles.top}>
        <View style={styles.dots}>
          {STEPS.map((s, i) => <View key={s} style={[styles.dot, i === step && styles.dotOn]} />)}
        </View>
        <Pressable accessibilityRole="button" accessibilityLabel="Skip" onPress={skip} hitSlop={10}>
          <Text style={[type.subhead, { color: colors.ink2 }]}>Skip</Text>
        </Pressable>
      </View>

      <View style={{ flex: 1, justifyContent: 'center' }}>
        {step === 0 ? (
          <>
            <View style={[styles.scoreOrb, askConsent && styles.scoreOrbSmall]}>
              <Text style={[styles.score, askConsent && { fontSize: 40 }]}>8.4</Text>
              <Text style={[type.footnote, { color: colors.ink2 }]}>out of 10</Text>
            </View>
            <Text style={[styles.title, { marginTop: askConsent ? 18 : 28 }]}>Get your swing rated by AI</Text>
            <View style={{ gap: askConsent ? 10 : 14, marginTop: askConsent ? 16 : 22 }}>
              <Row icon="gauge.with.needle" text="A score out of 10 in about a minute" />
              <Row icon="scope" text="Exactly what to fix, checked at every phase of your swing" />
              <Row icon="figure.golf" text="Drills picked for you, with video demos" />
            </View>
            <AnalyticsConsentCard style={{ marginTop: 20 }} />
          </>
        ) : null}

        {step === 1 ? (
          <>
            <Text style={styles.title}>How to film</Text>
            <View style={{ marginTop: 14 }}><FilmingIllustration /></View>
            <View style={{ gap: 12, marginTop: 14 }}>
              {FILMING_STEPS.map((s) => <Row key={s.text} icon={s.icon as SFSymbol} text={s.text} small />)}
            </View>
          </>
        ) : null}

        {step === 2 ? (
          <>
            <View style={[styles.scoreOrb, { backgroundColor: colors.goldSoft, borderColor: colors.goldLine }]}>
              <SymbolView name="play.rectangle.fill" size={40} tintColor={colors.gold} />
            </View>
            <Text style={[styles.title, { marginTop: 28 }]}>See an example</Text>
            <Text style={[type.callout, { color: colors.ink2, marginTop: 10, textAlign: 'center' }]}>
              {exampleReady === false
                ? "You're all set. Film a swing and you'll have your score in about a minute."
                : 'Watch a real analysis first, then film your own swing, or upload one you already have. Your first analysis is free.'}
            </Text>
          </>
        ) : null}
      </View>

      <View style={{ gap: 10 }}>
        {step < 2 ? (
          <Button label="Continue" block busy={checking} onPress={() => void next()} />
        ) : (
          <>
            {exampleReady !== false ? (
              <Button label="See the example" icon="play.fill" block busy={exampleReady === null} onPress={() => finish('example')} />
            ) : null}
            <Button label="Film my swing now" icon="video.fill" kind={exampleReady === false ? 'primary' : 'secondary'} block onPress={() => finish('film')} />
            <Button label="Not at the range? Upload a video" kind="quiet" block onPress={() => finish('upload')} />
          </>
        )}
      </View>
    </SafeArea>
  );
}

function Row({ icon, text, small }: { icon: SFSymbol; text: string; small?: boolean }) {
  return (
    <View style={{ flexDirection: 'row', gap: 12, alignItems: 'center' }}>
      <View style={styles.rowIcon}><SymbolView name={icon} size={small ? 15 : 17} tintColor={colors.gold} /></View>
      <Text style={[small ? type.footnote : type.callout, { color: colors.ink, flex: 1 }]}>{text}</Text>
    </View>
  );
}

const styles = themed(() => StyleSheet.create({
  screen: { flex: 1, backgroundColor: colors.bg, paddingHorizontal: space.gutter, paddingBottom: 12 },
  top: { flexDirection: 'row', alignItems: 'center', justifyContent: 'space-between', paddingVertical: 14 },
  dots: { flexDirection: 'row', gap: 6 },
  dot: { width: 8, height: 8, borderRadius: 4, backgroundColor: colors.surface3 },
  dotOn: { width: 22, backgroundColor: colors.gold },
  title: { ...type.title1, color: colors.ink, textAlign: 'center' },
  scoreOrb: { alignSelf: 'center', width: 150, height: 150, borderRadius: 75, borderWidth: 2, borderColor: colors.greenLine,
    backgroundColor: colors.greenSoft, alignItems: 'center', justifyContent: 'center' },
  scoreOrbSmall: { width: 112, height: 112, borderRadius: 56 },
  score: { fontSize: 52, fontWeight: '800', color: colors.gold, letterSpacing: -1.5 },
  rowIcon: { width: 34, height: 34, borderRadius: radius.md, backgroundColor: colors.goldSoft, alignItems: 'center', justifyContent: 'center' },
}));
