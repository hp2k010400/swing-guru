// Home: in-progress analyses, the latest swing, stats, the check most often out of range, checks in
// range per swing, recent swings, drills, milestones and filming tips. Everything is measured from the
// golfer's own analysed swings; no Swing Score until one is defined and calibrated.
import { Image } from 'expo-image';
import { router, useFocusEffect } from 'expo-router';
import { SymbolView, type SymbolViewProps } from 'expo-symbols';
import { useCallback, useEffect, useRef, useState, useSyncExternalStore } from 'react';
import { Pressable, ScrollView, StyleSheet, Text, useWindowDimensions, View } from 'react-native';

import { DrillCard } from '@/components/drill-card';
import { ChecksChart, Milestones, RecurringCard, StatTile } from '@/components/home';
import { SwingCard, swingDate } from '@/components/swing-card';
import { Button, Card, Caption, EmptyState, Screen, Section, StatusChip } from '@/components/ui';
import { pending, reconcilePending } from '@/lib/analysis';
import { api } from '@/lib/api';
import { openExample } from '@/lib/example';
import { cancelFirstSwingReminder } from '@/lib/reminders';
import { FirstSwingReminderCard } from '@/components/first-swing-reminder';
import { milestones, mostOftenOut, weekStreak } from '@/lib/home-stats';
import { ordinal, timeLeft } from '@/lib/league-view';
import { nextObjective } from '@/lib/progress-view';
import type { League, Progress, SwingDetail } from '@/lib/types';
import { AnalyticsConsentCard } from '@/components/analytics-consent';
import { RankBadge, TIER_ACCENT } from '@/components/rank-badge';
import { useSwings } from '@/lib/use-swings';
import { onboardingSeen, shouldShowOnboarding } from '@/lib/onboarding';
import { colors, radius, space, type, themed } from '@/theme';

const TIPS: [SymbolViewProps['name'], string][] = [
  ['arrow.up.and.down.and.arrow.left.and.right', 'Film down the line: behind your hands, looking along the target line.'],
  ['figure.golf', 'Keep your head, feet and the club in frame for the whole swing.'],
  ['speedometer', 'Record at 60 fps for a sharper club through impact.'],
];

function greeting(): string {
  const h = new Date().getHours();
  return h < 12 ? 'Good morning' : h < 18 ? 'Good afternoon' : 'Good evening';
}

export default function Home() {
  const { swings, error, reload } = useSwings();
  const inFlight = JSON.parse(useSyncExternalStore(pending.subscribe, () => JSON.stringify(pending.all()))) as ReturnType<typeof pending.all>;
  const { width } = useWindowDimensions();
  const latest = swings?.[0];
  const [details, setDetails] = useState<SwingDetail[]>([]);
  const [openDrill, setOpenDrill] = useState<string | null>(null);
  const [progress, setProgress] = useState<Progress | null>(null);
  // The phone's remembered uploads are checked against the server whenever Home is shown, so a
  // finished swing never keeps an "Upload stopped" card.
  useFocusEffect(useCallback(() => {
    void reconcilePending();
  }, []));
  const count = swings?.length ?? 0;
  // New golfers (no swings yet): the one-time onboarding, and the example analysis when the server has one.
  const empty = swings !== null && swings !== undefined && swings.length === 0;
  const [exampleReady, setExampleReady] = useState(false);
  const onboarded = useRef(false);
  useEffect(() => {
    if (!empty) return;
    api.example().then(() => setExampleReady(true)).catch(() => setExampleReady(false));
    if (!onboarded.current && shouldShowOnboarding(0, inFlight.length, onboardingSeen())) {
      onboarded.current = true;
      router.push('/onboarding');
    }
  }, [empty, inFlight.length]);
  // Any analysed swing (also one finished in the background): the first-swing reminder no longer applies.
  useEffect(() => {
    if (count) void cancelFirstSwingReminder();
  }, [count]);
  const [league, setLeague] = useState<League | null>(null);
  useEffect(() => {
    if (!count) return;
    api.progress().then(setProgress).catch(() => undefined);
    // Leagues exist on staging servers only for now; any error (404 on other servers) just hides the card.
    api.league().then(setLeague).catch(() => setLeague(null));
  }, [count]);
  useFocusEffect(useCallback(() => {
    if (count) api.league().then(setLeague).catch(() => undefined);    // after joining or a new week
  }, [count]));

  // The last few swings' checkpoints (for the recurring check, drills and milestones). This also warms
  // the Replay cache, so Replay from Home opens instantly.
  const recentIds = (swings ?? []).slice(0, 5).map((s) => s.id).join(',');
  useEffect(() => {
    if (!recentIds) return;
    let alive = true;
    Promise.all(recentIds.split(',').map((id) => api.swing(id).catch(() => null)))
      .then((list) => alive && setDetails(list.filter((d): d is SwingDetail => d !== null)))
      .catch(() => undefined);
    return () => {
      alive = false;
    };
  }, [recentIds]);
  const latestDetail = details.find((d) => d.id === latest?.id);
  const practice = latestDetail ? latestDetail.recommended.slice(0, 2) : [];
  const recurring = details.length ? mostOftenOut(details) : null;
  const badges = swings ? milestones(swings, details) : [];
  const earned = badges.filter((b) => b.earned).length;

  return (
    <Screen>
      <View style={{ paddingTop: 20, paddingBottom: 18 }}>
        <Text style={[type.footnote, { color: colors.ink2 }]}>{greeting()}</Text>
        <Text style={[type.title1, { color: colors.ink }]}>Swing Guru</Text>
      </View>

      {inFlight.map((job) => (
        <Card key={job.id} tone="tint" style={{ marginBottom: 12 }} onPress={() => router.push({ pathname: '/processing/[id]', params: { id: job.id } })}>
          <Caption style={{ color: job.phase === 'upload_failed' || job.phase === 'analysis_failed' ? colors.attentionInk : colors.greenInk }}>
            {job.phase === 'upload_failed' ? 'Upload stopped'
              : job.phase === 'analysis_failed' ? "Couldn't analyse a swing"
                : job.phase === 'purchase_required' ? 'Plan needed'
                  : job.phase === 'sign_in_required' ? 'Sign-in needed'
                    : job.phase === 'uploading' ? 'Uploading a swing'
                      : 'Analysing a swing'}
          </Caption>
          <Text style={[type.callout, { color: colors.ink, marginTop: 6 }]}>
            {job.phase === 'upload_failed'
              ? "Your video didn't finish uploading. Tap to try again."
              : job.phase === 'analysis_failed'
                ? 'Tap to see why and what to try.'
                : job.phase === 'purchase_required'
                  ? 'Your video is uploaded. Choose a plan to analyse it.'
                  : job.phase === 'sign_in_required'
                    ? 'Your video is uploaded, but free analyses need an account that stays with you.'
                    : 'Tap to see where it is up to.'}
          </Text>
        </Card>
      ))}

      {error ? (
        <Card tone="flat"><Text style={[type.callout, { color: colors.ink2 }]}>{error}</Text>
          <View style={{ marginTop: 10, alignItems: 'flex-start' }}><Button label="Try again" small kind="secondary" onPress={reload} /></View></Card>
      ) : null}

      {swings && !swings.length ? (
        <Card>
          <EmptyState icon="figure.golf" title="Analyze your first swing"
            text="Film one swing down the line. You'll see every checkpoint, and a drill for anything out of range."
            action={<Button label="Analyze a swing" icon="video.fill" onPress={() => router.push('/analyze')} />} />
          {exampleReady ? (
            <View style={{ alignItems: 'center', marginTop: 4 }}>
              <Button label="See an example analysis" icon="play.rectangle" kind="quiet" small
                onPress={() => openExample()} />
            </View>
          ) : null}
        </Card>
      ) : null}
      {/* After onboarding, still no swing of their own (and nothing uploading): offer one reminder. */}
      {swings && !swings.length && !inFlight.length && onboardingSeen() ? (
        <FirstSwingReminderCard from="home" ownSwings={0} />
      ) : null}

      {latest ? (
        <Card tone="hero" onPress={() => router.push({ pathname: '/swing/[id]', params: { id: latest.id } })}>
          <View style={{ flexDirection: 'row', gap: 14 }}>
            <View style={{ flex: 1 }}>
              <Caption style={{ color: 'rgba(255,255,255,0.7)' }}>Latest swing</Caption>
              <Text style={[type.headline, { color: colors.onAccent, fontSize: 20, marginTop: 8 }]}>{latest.club ?? 'Swing'} · {swingDate(latest)}</Text>
              {latest.score?.available && latest.score.total !== null ? (
                <View style={{ flexDirection: 'row', alignItems: 'baseline', gap: 8, marginTop: 10 }}>
                  <Text style={[type.number, { color: colors.onAccent, fontSize: 40 }]}>{latest.score.total.toFixed(1)}</Text>
                  <Text style={[type.subhead, { color: 'rgba(255,255,255,0.75)' }]}>
                    Swing Score{latest.counts ? `  ·  ${latest.counts.good} of ${latest.counts.total} in range` : ''}
                  </Text>
                </View>
              ) : latest.counts ? (
                <Text style={[type.subhead, { color: colors.onAccent, marginTop: 8 }]}>{latest.counts.good} of {latest.counts.total} checks in range</Text>
              ) : null}
            </View>
            {latest.thumb_url ? <Image source={{ uri: latest.thumb_url }} style={styles.heroThumb} contentFit="cover" /> : null}
          </View>
          {latest.summary_text ? <Text style={[type.footnote, { color: 'rgba(255,255,255,0.75)', marginTop: 10 }]} numberOfLines={3}>{latest.summary_text}</Text> : null}
          <View style={{ flexDirection: 'row', gap: 10, marginTop: 16 }}>
            <Button label="View analysis" kind="light" small onPress={() => router.push({ pathname: '/swing/[id]', params: { id: latest.id } })} />
            <Button label="Replay" kind="light" icon="play.fill" small onPress={() => router.push({ pathname: '/replay/[id]', params: { id: latest.id } })} />
          </View>
        </Card>
      ) : null}

      {latest ? (
        <Pressable accessibilityRole="button" accessibilityLabel="Analyse a new swing" onPress={() => router.push('/analyze')}
          style={({ pressed }) => [styles.action, pressed && { backgroundColor: colors.surface2 }]}>
          <View style={styles.actionIcon}>
            <SymbolView name="video.fill" size={22} tintColor={colors.green} />
          </View>
          <View style={{ flex: 1 }}>
            <Text style={[type.headline, { color: colors.ink }]}>Analyse a new swing</Text>
            <Text style={[type.footnote, { color: colors.ink2, marginTop: 2 }]}>Record or choose a video · about a minute</Text>
          </View>
          <SymbolView name="chevron.right" size={15} tintColor={colors.ink3} />
        </Pressable>
      ) : null}
      {latest && progress && progress.rank.eligible_swings ? <RankCard progress={progress} /> : null}
      {latest && league ? <LeagueCard league={league} /> : null}
      {swings && swings.length >= 2 ? (
        <Pressable accessibilityRole="button" accessibilityLabel="Compare two swings" onPress={() => router.push('/compare')}
          style={({ pressed }) => [styles.action, pressed && { backgroundColor: colors.surface2 }]}>
          <View style={styles.actionIcon}>
            <SymbolView name="square.split.2x1.fill" size={22} tintColor={colors.greenInk} />
          </View>
          <View style={{ flex: 1 }}>
            <Text style={[type.headline, { color: colors.ink }]}>Compare swings</Text>
            <Text style={[type.footnote, { color: colors.ink2, marginTop: 2 }]}>See what genuinely improved between two swings</Text>
          </View>
          <SymbolView name="chevron.right" size={15} tintColor={colors.ink3} />
        </Pressable>
      ) : null}
      <AnalyticsConsentCard />
      {latest && swings ? (
        <View style={{ flexDirection: 'row', gap: 10, marginTop: space.section }}>
          <StatTile icon="figure.golf" value={String(swings.length)} label="Analysed" onPress={() => router.push('/library')} />
          <StatTile icon="flame.fill" value={String(weekStreak(swings))} label="Week streak" />
          <StatTile icon="checkmark.circle" value={latest.counts ? `${latest.counts.good}/${latest.counts.total}` : '–'} label="In range"
            onPress={() => router.push({ pathname: '/swing/[id]', params: { id: latest.id } })} />
        </View>
      ) : null}

      {recurring ? <View style={{ marginTop: 14 }}><RecurringCard item={recurring} /></View> : null}

      {swings && swings.length ? <View style={{ marginTop: 14 }}><ChecksChart swings={swings} /></View> : null}

      {swings && swings.length ? (
        <Section title="Recent swings" action={<Button label="View all" kind="quiet" small onPress={() => router.push('/library')} />}>
          <ScrollView horizontal showsHorizontalScrollIndicator={false} contentContainerStyle={{ gap: 12, paddingRight: space.gutter }}
            style={{ marginHorizontal: -space.gutter, paddingLeft: space.gutter }}>
            {swings.slice(0, 8).map((s) => <SwingCard key={s.id} swing={s} width={Math.round(width * 0.42)} />)}
          </ScrollView>
        </Section>
      ) : null}


      {practice.length ? (
        <Section title="Practise next" action={<Button label="All drills" kind="quiet" small onPress={() => router.push('/drills')} />}>
          {practice.map(({ drill, checkpoint }) => (
            <DrillCard key={drill.id} drill={drill} open={openDrill === drill.id} onToggle={() => setOpenDrill(openDrill === drill.id ? null : drill.id)}
              chip={<StatusChip label={`${checkpoint.name}: ${checkpoint.label}`} status={checkpoint.status} />} />
          ))}
          <Text style={[type.footnote, { color: colors.ink3 }]}>From your latest swing · preview drills, not yet reviewed by a coach</Text>
        </Section>
      ) : null}

      {badges.length && latest ? (
        <Section title="Milestones" action={<Text style={[type.subhead, { color: colors.green }]}>{earned}/{badges.length}</Text>}>
          <Milestones items={badges} />
        </Section>
      ) : null}

      {latest ? (
        <Section title="Filming tips">
          <Card tone="flat">
            {TIPS.map(([icon, text], i) => (
              <View key={text} style={[styles.tip, i > 0 && { marginTop: 12 }]}>
                <SymbolView name={icon} size={18} tintColor={colors.green} />
                <Text style={[type.callout, { color: colors.ink2, flex: 1 }]}>{text}</Text>
              </View>
            ))}
          </Card>
        </Section>
      ) : null}
    </Screen>
  );
}

/** The Swing Guru Rank at a glance; opens Progress. */
function RankCard({ progress }: { progress: Progress }) {
  const r = progress.rank;
  const next = r.next;
  return (
    <Pressable accessibilityRole="button" accessibilityLabel={`Your rank: ${r.rank.name}${r.provisional ? ', provisional' : ''}. Open progress`}
      onPress={() => router.push('/progress')}
      style={({ pressed }) => [styles.rank, { borderColor: r.provisional ? colors.line : colors.goldLine }, pressed && { opacity: 0.9 }]}>
      <RankBadge tier={r.rank.tier} division={r.rank.division} size={64} locked={r.provisional} />
      <View style={{ flex: 1 }}>
        <Caption style={{ color: r.provisional ? colors.ink3 : colors.gold }}>{r.provisional ? 'Provisional rank' : 'Your rank'}</Caption>
        <Text style={[type.title2, { color: colors.ink, marginTop: 2 }]}>{r.rank.name}</Text>
        {r.provisional ? (
          <Text style={[type.footnote, { color: colors.ink2, marginTop: 4 }]}>{r.provisional_remaining} more {r.provisional_remaining === 1 ? 'swing' : 'swings'} to confirm</Text>
        ) : next ? (
          <View style={styles.rankTrack}>
            <View style={{ width: `${Math.round((nextObjective(r)?.overall ?? 0) * 100)}%`, height: 5, borderRadius: 3, backgroundColor: TIER_ACCENT[r.rank.tier] }} />
          </View>
        ) : null}
      </View>
      <SymbolView name="chevron.right" size={15} tintColor={colors.ink3} />
    </Pressable>
  );
}

/** The weekly league at a glance (or an invitation to join); opens the League screen. */
function LeagueCard({ league }: { league: League }) {
  const you = league.you;
  const g = league.group;
  const title = league.joined && g && you ? `${ordinal(you.position)} in ${g.division}` : 'Weekly league';
  const sub = league.joined && you ? `${you.lp} LP · ${timeLeft(league.week.end)}`
    : league.can_join ? 'Join a group of 20 and compete each week'
      : `Analyse ${Math.max(0, (league.join_min_swings ?? 5) - (league.counted_swings ?? 0))} more swings to join`;
  return (
    <Pressable accessibilityRole="button" accessibilityLabel={`${title}. ${sub}. Open the league`} onPress={() => router.push('/league')}
      style={({ pressed }) => [styles.action, pressed && { backgroundColor: colors.surface2 }]}>
      <View style={[styles.actionIcon, { backgroundColor: colors.goldSoft }]}>
        <SymbolView name="trophy.fill" size={22} tintColor={colors.gold} />
      </View>
      <View style={{ flex: 1 }}>
        <Text style={[type.headline, { color: colors.ink }]}>{title}</Text>
        <Text style={[type.footnote, { color: colors.ink2, marginTop: 2 }]}>{sub}</Text>
      </View>
      <SymbolView name="chevron.right" size={15} tintColor={colors.ink3} />
    </Pressable>
  );
}

const styles = themed(() => StyleSheet.create({
  rank: { flexDirection: 'row', alignItems: 'center', gap: 14, padding: 14, marginTop: 12, borderRadius: radius.lg, borderWidth: 1, backgroundColor: colors.surface },
  rankTrack: { height: 5, borderRadius: 3, backgroundColor: colors.surface3, overflow: 'hidden', marginTop: 8 },
  heroThumb: { width: 64, height: 86, borderRadius: radius.sm, backgroundColor: 'rgba(255,255,255,0.12)' },
  action: { flexDirection: 'row', alignItems: 'center', gap: 14, padding: 16, marginTop: 12, borderRadius: radius.lg, borderWidth: 1, borderColor: colors.line, backgroundColor: colors.surface },
  actionIcon: { width: 46, height: 46, borderRadius: 14, backgroundColor: colors.greenSoft, alignItems: 'center', justifyContent: 'center' },
  tip: { flexDirection: 'row', alignItems: 'center', gap: 12 },
}));
