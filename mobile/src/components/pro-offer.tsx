// The Pro offer on a golfer's first result (just finished, or their only swing opened from anywhere; never
// the example), only for golfers without Pro.
//
// The FIRST successful result opens the full-screen trial paywall once, a few seconds after the score has
// been revealed (most trials start on install day; a dismissible card was too easy to miss). It never
// covers the score itself, has a clear "Not now", and never follows a failed analysis (this screen only
// exists for a completed one). Later results show the card below instead.
// 1.0.7: never auto-opened for someone who already started a trial (in onboarding, on another device, or
// earlier on this Apple ID).
//
// The free-trial wording is only shown when the App Store says THIS Apple ID is eligible for the trial
// (same eligibility check as the paywall), and the number of days comes from the store offer.
import { router } from 'expo-router';
import { useEffect, useState } from 'react';
import { Text, View } from 'react-native';

import { billingSupported, loadOffers, serverStatus } from '@/lib/billing';
import { shouldAutoOpenPaywall, trialAlreadyStarted } from '@/lib/first-result';
import { userId } from '@/lib/session';
import { trialRecordedHere } from '@/lib/trial-flag';
import { colors, type } from '@/theme';

import { Button, Caption, Card } from './ui';

const SEEN = 'sg.paywall.firstResult';
const DELAY_MS = 3500; // after the score reveal

function seenBefore(): boolean {
  try {
    return localStorage.getItem(SEEN) !== null;
  } catch {
    return true; // storage unavailable: don't risk opening it on every result
  }
}

export function ProOfferCard({ fix }: { fix?: string | null }) {
  const [show, setShow] = useState(false);
  const [trialDays, setTrialDays] = useState<number | null>(null);
  const open = () => router.push({ pathname: '/paywall', params: fix ? { from: 'first_result', fix } : { from: 'first_result' } });
  useEffect(() => {
    if (!billingSupported) return;
    let alive = true;
    let timer: ReturnType<typeof setTimeout> | undefined;
    const startedAt = Date.now();
    (async () => {
      try {
        const status = await serverStatus();
        if (!alive || !status.enabled || status.access === 'pro') return;
        setShow(true);
        // Only an eligible free trial on a subscription changes the wording. The offers also say whether
        // this Apple ID already used its trial, which (like a trial started here) skips the auto-open.
        let offers: Awaited<ReturnType<typeof loadOffers>>['offers'] = [];
        try {
          offers = (await loadOffers(await userId())).offers;
        } catch {
          offers = [];
        }
        if (!alive) return;
        setTrialDays(offers.find((o) => o.trial)?.trial?.days ?? null);
        const trialStarted = trialAlreadyStarted({
          recordedHere: trialRecordedHere(), access: status.access, eligibilities: offers.map((o) => o.eligibility),
        });
        if (shouldAutoOpenPaywall({ enabled: status.enabled, access: status.access, seenBefore: seenBefore(), trialStarted })) {
          try {
            localStorage.setItem(SEEN, new Date().toISOString());
          } catch {
            /* seenBefore() already treats missing storage as seen */
          }
          timer = setTimeout(() => alive && open(), Math.max(0, DELAY_MS - (Date.now() - startedAt)));
        }
      } catch {
        /* card stays hidden, or shows the plain wording */
      }
    })();
    return () => {
      alive = false;
      if (timer) clearTimeout(timer);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);
  if (!show) return null;
  return (
    <Card tone="tint" style={{ marginTop: 16 }}>
      <Caption style={{ color: colors.greenInk }}>Swing Guru Pro</Caption>
      <Text style={[type.headline, { color: colors.ink, marginTop: 6 }]}>Keep improving after this swing</Text>
      <Text style={[type.callout, { color: colors.ink2, marginTop: 6 }]}>
        Unlimited analyses, your progress over time and weekly leagues.
        {trialDays ? ` Free for ${trialDays} days, then your plan renews unless you cancel.` : ''}
      </Text>
      <View style={{ flexDirection: 'row', flexWrap: 'wrap', gap: 8, marginTop: 12 }}>
        <Button label={trialDays ? `Start your free ${trialDays}-day trial` : 'See Pro plans'} icon="sparkles" small onPress={open} />
        <Button label="Not now" kind="quiet" small onPress={() => setShow(false)} />
      </View>
    </Card>
  );
}
