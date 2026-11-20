// Progress: the Swing Guru Rank, score history, personal bests, trend, consistency and practice.
// Everything comes from GET /v1/progress (the golfer's own analysed swings); nothing is invented here.
import { router } from 'expo-router';
import { SymbolView } from 'expo-symbols';
import { useCallback, useEffect, useState } from 'react';
import { ActivityIndicator, Pressable, ScrollView, StyleSheet, Text, useWindowDimensions, View } from 'react-native';
import Svg, { Circle, Defs, Line, LinearGradient, Path, Stop, Text as SvgText } from 'react-native-svg';

import { RankBadge, RankCelebration, TIER_ACCENT } from '@/components/rank-badge';
import { Button, Caption, Card, EmptyState, SafeArea, Section } from '@/components/ui';
import { track } from '@/lib/analytics';
import { api, friendly } from '@/lib/api';
import {
  achievements, consistency, nextObjective, notCounted, practice, rollingMedian, scoredSeries, shouldCelebrate, trend,
} from '@/lib/progress-view';
import { lastSeenRank, rememberRank } from '@/lib/rank-seen';
import type { Progress, RankInfo } from '@/lib/types';
import { colors, radius, space, type, themed } from '@/theme';

const TIERS = ['Bronze', 'Silver', 'Gold', 'Platinum', 'Diamond'] as const;

export default function ProgressScreen() {
  const [data, setData] = useState<Progress | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [celebrate, setCelebrate] = useState<RankInfo['rank'] | null>(null);
  const load = useCallback(() => {
    api.progress().then((p) => {
      setError(null);
      setData(p);
      track('progress_viewed', { rank: p.rank.rank.name, provisional: p.rank.provisional });
      const seen = lastSeenRank();
      if (!p.rank.provisional && shouldCelebrate(p, seen)) {
        if (seen !== null) track('rank_progressed', { from_rank: seen, to_rank: p.rank.rank.index });
        setCelebrate(p.rank.rank);
      }
      if (!p.rank.provisional) rememberRank(p.rank.rank.index);
    }).catch((e) => setError(friendly(e)));
  }, []);
  useEffect(load, [load]);
  // The league entry shows only where the server has leagues (staging for now).
  const [leagueOn, setLeagueOn] = useState(false);
  useEffect(() => {
    api.league().then(() => setLeagueOn(true)).catch(() => setLeagueOn(false));
  }, []);

  return (
    <SafeArea style={styles.screen} edges={['top']}>
      <View style={styles.top}>
        <Pressable accessibilityRole="button" accessibilityLabel="Close" onPress={() => (router.canGoBack() ? router.back() : router.replace('/'))}
          hitSlop={8} style={styles.round}>
          <SymbolView name="xmark" size={16} tintColor={colors.ink} />
        </Pressable>
        <Text style={[type.headline, { color: colors.ink }]}>Your progress</Text>
        <View style={{ width: 44 }} />
      </View>
      {error ? (
        <EmptyState icon="wifi.exclamationmark" title="Couldn't load your progress" text={error} action={<Button label="Try again" onPress={load} />} />
      ) : !data ? (
        <View style={{ flex: 1, alignItems: 'center', justifyContent: 'center' }}><ActivityIndicator color={colors.green} /></View>
      ) : !data.swings.length ? (
        <EmptyState icon="chart.line.uptrend.xyaxis" title="Your progress starts here"
          text="Analyse a few swings and your rank, score history and personal bests appear here."
          action={<Button label="Analyse a swing" icon="video.fill" onPress={() => router.push('/analyze')} />} />
      ) : (
        <ScrollView contentContainerStyle={styles.scroll} showsVerticalScrollIndicator={false}>
          <RankHero rank={data.rank} />
          {leagueOn ? (
            <View style={{ marginTop: 12 }}>
              <Button label="Weekly league" icon="trophy.fill" kind="secondary" block onPress={() => router.push('/league')} />
            </View>
          ) : null}
          <Stats data={data} />
          <ScoreHistory data={data} />
          <Consistency data={data} />
          <Achievements rank={data.rank} />
          <Ladder rank={data.rank} />
          <Fairness rank={data.rank} />
          <Text style={[type.footnote, { color: colors.ink3, textAlign: 'center', marginTop: 24 }]}>{data.rank.disclaimer}</Text>
        </ScrollView>
      )}
      <RankCelebration rank={celebrate} onClose={() => setCelebrate(null)} />
    </SafeArea>
  );
}

function RankHero({ rank }: { rank: RankInfo }) {
  const accent = TIER_ACCENT[rank.rank.tier];
  const goal = nextObjective(rank);
  return (
    <View style={[styles.hero, { borderColor: rank.provisional ? colors.line : colors.goldLine }]}>
      <View style={{ flexDirection: 'row', alignItems: 'center', gap: 16 }}>
        <RankBadge tier={rank.rank.tier} division={rank.rank.division} size={104} locked={rank.provisional} />
        <View style={{ flex: 1, gap: 2 }}>
          <Caption style={{ color: rank.provisional ? colors.ink3 : colors.gold }}>{rank.provisional ? 'Provisional rank' : 'Current rank'}</Caption>
          <Text style={[type.title1, { color: colors.ink }]} numberOfLines={1} adjustsFontSizeToFit>{rank.rank.name}</Text>
          {rank.provisional ? (
            <Text style={[type.footnote, { color: colors.ink2 }]}>
              {rank.provisional_remaining} more counted {rank.provisional_remaining === 1 ? 'swing' : 'swings'} to confirm your rank.
            </Text>
          ) : goal && rank.next ? (
            <Text style={[type.footnote, { color: colors.ink2 }]}>Next rank: <Text style={{ color: colors.ink, fontWeight: '600' }}>{rank.next.name}</Text></Text>
          ) : (
            <Text style={[type.footnote, { color: colors.ink2 }]}>The top rank. Outstanding.</Text>
          )}
        </View>
      </View>
      {!rank.provisional && goal ? (
        <View style={{ marginTop: 18 }}>
          <View style={styles.track}><View style={[styles.fill, { width: `${Math.round(goal.overall * 100)}%`, backgroundColor: accent }]} /></View>
          <Requirement label="Points" met={goal.points.met} progress={goal.points.progress} accent={accent}
            value={goal.points.met ? 'Requirement complete' : `${goal.points.current.toLocaleString('en-GB')} / ${goal.points.required.toLocaleString('en-GB')}`} />
          {goal.score ? (
            <Requirement label="Typical score" met={goal.score.met} progress={goal.score.progress} accent={accent}
              value={`${goal.score.current !== null ? goal.score.current.toFixed(1) : '–'} / ${goal.score.required.toFixed(1)}`} />
          ) : null}
          {goal.weeks ? (
            <Requirement label="Weekly checks in a row" met={goal.weeks.met >= goal.weeks.required}
              progress={goal.weeks.met / goal.weeks.required} accent={accent} value={`${goal.weeks.met} of ${goal.weeks.required}`} />
          ) : null}
          {goal.tenure ? (
            <Requirement label="Time and practice" met={goal.tenure.met} accent={accent}
              progress={Math.min(goal.tenure.weeksDone / goal.tenure.weeksRequired, goal.tenure.swingsDone / goal.tenure.swingsRequired)}
              value={`${Math.min(goal.tenure.weeksDone, goal.tenure.weeksRequired)}/${goal.tenure.weeksRequired} wks · ${Math.min(goal.tenure.swingsDone, goal.tenure.swingsRequired)}/${goal.tenure.swingsRequired} swings`} />
          ) : null}
          {goal.weeks?.nextCheck ? (
            <Text style={[type.footnote, { color: colors.ink3, marginTop: 10 }]}>
              Next weekly check: {new Date(goal.weeks.nextCheck).toLocaleDateString('en-GB', { weekday: 'short', day: 'numeric', month: 'short' })}
            </Text>
          ) : null}
          <Text style={[type.callout, { color: colors.ink, marginTop: 14 }]}>{goal.message}</Text>
        </View>
      ) : null}
    </View>
  );
}

/** One promotion requirement: what it is, where you are, and whether it's done. */
function Requirement({ label, value, met, progress, accent }: { label: string; value: string; met: boolean; progress: number; accent: string }) {
  return (
    <View style={styles.requirement} accessibilityLabel={`${label}: ${value}${met ? ', complete' : ''}`}>
      <View style={[styles.reqIcon, { backgroundColor: met ? colors.goodSoft : colors.surface2 }]}>
        <SymbolView name={met ? 'checkmark' : 'circle.dotted'} size={12} tintColor={met ? colors.goodInk : colors.ink3} weight="bold" />
      </View>
      <Text style={[type.subhead, { color: colors.ink2, flex: 1 }]}>{label}</Text>
      <View style={{ alignItems: 'flex-end', gap: 4 }}>
        <Text style={[type.subhead, { color: met ? colors.goodInk : colors.ink, fontWeight: '700', fontVariant: ['tabular-nums'] }]}>{value}</Text>
        {!met ? (
          <View style={[styles.track, { width: 96, height: 4, marginTop: 0 }]}>
            <View style={{ width: `${Math.round(progress * 100)}%`, height: 4, borderRadius: 2, backgroundColor: accent }} />
          </View>
        ) : null}
      </View>
    </View>
  );
}

function Stats({ data }: { data: Progress }) {
  const t = trend(data.swings);
  const p = practice(data.swings);
  const tiles: { label: string; value: string; note?: string; gold?: boolean }[] = [
    { label: 'Personal best', value: data.personal_bests.score ? data.personal_bests.score.score.toFixed(1) : '–', gold: true },
    { label: 'Typical score', value: data.rank.rating !== null ? data.rank.rating.toFixed(1) : '–', note: 'median, last 10' },
    { label: 'Recent trend', value: t ? `${t.delta > 0 ? '+' : ''}${t.delta.toFixed(1)}` : '–', note: t ? 'last 5 vs 5 before' : 'needs 10 swings' },
    { label: 'This week', value: String(p.thisWeek), note: `${p.weekStreak}-week streak` },
  ];
  return (
    <View style={styles.tiles}>
      {tiles.map((tile) => (
        <View key={tile.label} style={styles.tile}>
          <Text style={[type.caption, { color: colors.ink3 }]}>{tile.label}</Text>
          <Text style={[type.number, { fontSize: 30, color: tile.gold ? colors.gold : colors.ink, marginTop: 6 }]}>{tile.value}</Text>
          {tile.note ? <Text style={[type.footnote, { color: colors.ink3, marginTop: 2 }]}>{tile.note}</Text> : null}
        </View>
      ))}
    </View>
  );
}

function ScoreHistory({ data }: { data: Progress }) {
  const { width } = useWindowDimensions();
  const points = scoredSeries(data.swings, 24);
  const [picked, setPicked] = useState<number | null>(null);
  if (points.length < 2) return null;
  const axis = 30, w = width - space.gutter * 2 - 40, plot = w - axis, h = 170, pad = 10;
  const scores = points.map((p) => p.score);
  const lo = Math.max(0, Math.floor(Math.min(...scores) - 0.3)), hi = Math.min(10, Math.ceil(Math.max(...scores) + 0.1));
  const ticks = [lo, (lo + hi) / 2, hi];
  const x = (i: number) => axis + pad + (i * (plot - 2 * pad)) / (points.length - 1);
  const y = (v: number) => pad + (1 - (v - lo) / (hi - lo || 1)) * (h - 2 * pad);
  const line = points.map((p, i) => `${i ? 'L' : 'M'}${x(i).toFixed(1)} ${y(p.score).toFixed(1)}`).join(' ');
  const area = `${line} L${x(points.length - 1).toFixed(1)} ${h} L${x(0).toFixed(1)} ${h} Z`;
  const trendLine = rollingMedian(scores, 5).map((v, i) => (v === null ? null : `${x(i).toFixed(1)} ${y(v).toFixed(1)}`))
    .filter(Boolean).map((p, i) => `${i ? 'L' : 'M'}${p}`).join(' ');
  const best = data.personal_bests.score?.swing_id;
  const sel = picked !== null ? points[picked] : null;
  const pick = (locationX: number) => {
    const i = Math.round(((locationX - axis - pad) / (plot - 2 * pad)) * (points.length - 1));
    setPicked(Math.max(0, Math.min(points.length - 1, i)));
  };
  const fmt = (iso: string) => new Date(iso).toLocaleDateString('en-GB', { day: 'numeric', month: 'short' });
  return (
    <Section title="Score history">
      <Card>
        <Pressable accessibilityRole="adjustable" accessibilityLabel={`Score history, ${points.length} swings. Tap a point for details.`}
          onPress={(e) => pick(e.nativeEvent.locationX)}>
          <Svg width={w} height={h}>
            <Defs>
              <LinearGradient id="histFill" x1="0" y1="0" x2="0" y2="1">
                <Stop offset="0" stopColor={colors.green} stopOpacity={0.3} />
                <Stop offset="1" stopColor={colors.green} stopOpacity={0} />
              </LinearGradient>
            </Defs>
            {ticks.map((t) => (
              <Line key={t} x1={axis} x2={w} y1={y(t)} y2={y(t)} stroke={colors.line} strokeWidth={1} />
            ))}
            {ticks.map((t) => (
              <SvgText key={`l${t}`} x={axis - 6} y={y(t) + 4} fontSize={11} fill={colors.ink3} textAnchor="end">{t.toFixed(t % 1 ? 1 : 0)}</SvgText>
            ))}
            <Path d={area} fill="url(#histFill)" />
            <Path d={line} stroke={colors.green} strokeWidth={2.2} fill="none" strokeLinejoin="round" strokeLinecap="round" />
            {trendLine ? <Path d={trendLine} stroke={colors.ink2} strokeWidth={1.5} strokeDasharray="4 4" fill="none" /> : null}
            {points.map((p, i) => (
              <Circle key={p.id} cx={x(i)} cy={y(p.score)} r={p.id === best ? 5.5 : picked === i ? 4.5 : 2.6}
                fill={p.id === best ? colors.gold : picked === i ? colors.ink : colors.green}
                stroke={colors.surface} strokeWidth={p.id === best || picked === i ? 2 : 0} />
            ))}
            {picked !== null ? <Line x1={x(picked)} x2={x(picked)} y1={pad} y2={h} stroke={colors.lineStrong} strokeWidth={1} /> : null}
          </Svg>
        </Pressable>
        <View style={{ flexDirection: 'row', justifyContent: 'space-between', marginTop: 6, paddingLeft: axis }}>
          <Text style={[type.footnote, { color: colors.ink3 }]}>{fmt(points[0].created_at)}</Text>
          <Text style={[type.footnote, { color: colors.ink3 }]}>{fmt(points[points.length - 1].created_at)}</Text>
        </View>
        {sel ? (
          <View style={styles.pointCard}>
            <View style={{ flex: 1 }}>
              <Text style={[type.headline, { color: colors.ink }]}>
                {sel.score.toFixed(1)} <Text style={[type.footnote, { color: colors.ink2 }]}>· {sel.club ?? 'Swing'} · {fmt(sel.created_at)}</Text>
              </Text>
              {sel.id === best ? <Text style={[type.footnote, { color: colors.gold, fontWeight: '700', marginTop: 2 }]}>Personal best</Text> : null}
            </View>
            <Button label="Open" small kind="secondary" onPress={() => router.push({ pathname: '/swing/[id]', params: { id: sel.id } })} />
          </View>
        ) : null}
        <View style={{ flexDirection: 'row', gap: 16, marginTop: 10, flexWrap: 'wrap' }}>
          <Legend swatch={<View style={{ width: 8, height: 8, borderRadius: 4, backgroundColor: colors.gold }} />} label="Personal best" />
          <Legend swatch={<View style={{ width: 14, borderTopWidth: 1.5, borderStyle: 'dashed', borderColor: colors.ink2 }} />} label="Trend (5 swings)" />
          <Text style={[type.footnote, { color: colors.ink3 }]}>Tap a point for details</Text>
        </View>
      </Card>
    </Section>
  );
}

function Legend({ swatch, label }: { swatch: React.ReactNode; label: string }) {
  return (
    <View style={{ flexDirection: 'row', alignItems: 'center', gap: 6 }}>
      {swatch}
      <Text style={[type.footnote, { color: colors.ink3 }]}>{label}</Text>
    </View>
  );
}

function Consistency({ data }: { data: Progress }) {
  const rows = consistency(data.swings);
  if (!rows.length) return null;
  return (
    <Section title="Consistency">
      <Card>
        <Text style={[type.footnote, { color: colors.ink2, marginBottom: 12 }]}>How often each check was in range over your last 10 swings that measured it.</Text>
        {rows.map((r) => {
          const share = r.inRange / r.measured;
          return (
            <View key={r.id} style={{ marginBottom: 12 }}>
              <View style={{ flexDirection: 'row', justifyContent: 'space-between' }}>
                <Text style={[type.subhead, { color: colors.ink }]}>{r.name}</Text>
                <Text style={[type.subhead, { color: colors.ink2, fontVariant: ['tabular-nums'] }]}>{r.inRange}/{r.measured}</Text>
              </View>
              <View style={[styles.track, { marginTop: 6 }]}>
                <View style={[styles.fill, { width: `${Math.round(share * 100)}%`, backgroundColor: share >= 0.7 ? colors.good : share >= 0.4 ? colors.attention : colors.problem }]} />
              </View>
            </View>
          );
        })}
      </Card>
    </Section>
  );
}

function Achievements({ rank }: { rank: RankInfo }) {
  const list = achievements(rank).slice(0, 6);
  if (!list.length) return null;
  return (
    <Section title="Achievements">
      <Card>
        {list.map((a, i) => (
          <View key={a.key} style={[styles.achievement, i > 0 && { borderTopWidth: StyleSheet.hairlineWidth, borderTopColor: colors.line }]}>
            <View style={styles.medal}><SymbolView name="rosette" size={18} tintColor={colors.gold} /></View>
            <Text style={[type.subhead, { color: colors.ink, flex: 1 }]}>{a.title}</Text>
            <Text style={[type.footnote, { color: colors.ink3 }]}>{new Date(a.date).toLocaleDateString('en-GB', { day: 'numeric', month: 'short' })}</Text>
          </View>
        ))}
      </Card>
    </Section>
  );
}

function Ladder({ rank }: { rank: RankInfo }) {
  const tierIndex = TIERS.indexOf(rank.rank.tier);
  return (
    <Section title="The ranks">
      <ScrollView horizontal showsHorizontalScrollIndicator={false} contentContainerStyle={{ gap: 14, paddingRight: space.gutter }}>
        {TIERS.map((tier, i) => (
          <View key={tier} style={{ alignItems: 'center', width: 72 }}>
            <RankBadge tier={tier} division={i < tierIndex ? 'I' : i === tierIndex ? rank.rank.division : 'III'} size={60} locked={i > tierIndex} />
            <Text style={[type.footnote, { color: i <= tierIndex ? colors.ink : colors.ink3, marginTop: 6, fontWeight: '600' }]}>{tier}</Text>
            {rank.config.tier_rating[tier] ? (
              <Text style={[type.footnote, { color: colors.ink3, fontSize: 11 }]}>score {rank.config.tier_rating[tier].toFixed(1)}+</Text>
            ) : <Text style={[type.footnote, { color: colors.ink3, fontSize: 11 }]}>start</Text>}
          </View>
        ))}
      </ScrollView>
    </Section>
  );
}

function Fairness({ rank }: { rank: RankInfo }) {
  const skipped = notCounted(rank);
  const p = rank.config.points;
  const c = rank.config;
  const v2 = Array.isArray(c.day_swings);
  const rows: [string | undefined, string][] = v2 ? [
    [c.day_swings!.join(' / '), `Your first ${c.day_swings!.length} swings each day (no points after that)`],
    [String(p.personal_best), 'Set a new personal best (once a week)'],
    [String(p.back_in_range), 'Bring a check back into range, confirmed on your next swing'],
    [String(p.practice_week), `Practise on ${c.practice_week_swings ?? 3} or more days in a week`],
  ] : [
    [String(p.swing), 'Analyse a new eligible swing'],
    [String(p.personal_best), 'Set a new personal best'],
    [String(p.back_in_range), 'Bring a check back into range'],
    [String(p.practice_week), `Complete a practice week (${c.practice_week_swings ?? 3}+ swings)`],
  ];
  const pct = Math.round((1 - (c.rating_percentile ?? 0.3)) * 100);
  const notes = v2 ? [
    `Typical score: the level ${pct}% of your last ${c.rating_window ?? 20} eligible swings beat (needs ${c.rating_min_swings ?? 10}).`,
    `New tiers open at the weekly check (Monday), after ${c.gate_weeks ?? 2} qualifying weeks in a row. Platinum and Diamond also need time and practice.`,
    `Swings with fewer than ${c.min_measured ?? 6} checks measured, or videos under ${c.min_long_side ?? 960} px, earn practice points but don't count towards your typical score.`,
    'Uploading the same swing again earns nothing.',
    `New golfers get a provisional rank until ${c.provisional_swings ?? 5} swings have counted.`,
  ] : [
    `Up to ${c.daily_cap} swings count each day.`,
    'Uploading the same swing again earns no extra points.',
    `Moving up a tier also needs your typical score (the median of your last ${c.rating_window ?? 10} counted swings) to reach that tier.`,
    `New golfers get a provisional rank until ${c.provisional_swings ?? 5} swings have counted.`,
  ];
  return (
    <Section title="How points work">
      <Card tone="flat">
        {rows.filter(([pts]) => pts !== undefined).map(([pts, label], i) => (
          <View key={label} style={[styles.pointRow, i > 0 && { borderTopWidth: StyleSheet.hairlineWidth, borderTopColor: colors.line }]}>
            <Text style={[styles.points, { color: colors.gold }]}>+{pts}</Text>
            <Text style={[type.callout, { color: colors.ink, flex: 1, flexShrink: 1 }]}>{label}</Text>
          </View>
        ))}
        <View style={{ marginTop: 12, gap: 6 }}>
          {notes.map((n) => (
            <View key={n} style={{ flexDirection: 'row', gap: 8 }}>
              <Text style={[type.footnote, { color: colors.ink3 }]}>•</Text>
              <Text style={[type.footnote, { color: colors.ink2, flex: 1, flexShrink: 1 }]}>{n}</Text>
            </View>
          ))}
        </View>
        {skipped.duplicate || skipped.dailyCap ? (
          <Text style={[type.footnote, { color: colors.ink3, marginTop: 10 }]}>
            {[skipped.duplicate ? `${skipped.duplicate} re-uploaded ${skipped.duplicate === 1 ? 'swing' : 'swings'}` : null,
              skipped.dailyCap ? `${skipped.dailyCap} over the daily limit` : null].filter(Boolean).join(' and ')} didn&apos;t earn points.
          </Text>
        ) : null}
        {rank.eligibility?.practice_only ? (
          <Text style={[type.footnote, { color: colors.ink3, marginTop: 6 }]}>
            {rank.eligibility.practice_only} {rank.eligibility.practice_only === 1 ? 'swing was' : 'swings were'} practice only (too few checks measured, or a low-resolution video), so {rank.eligibility.practice_only === 1 ? 'it isn’t' : 'they aren’t'} in your typical score.
          </Text>
        ) : null}
      </Card>
    </Section>
  );
}

const styles = themed(() => StyleSheet.create({
  screen: { flex: 1, backgroundColor: colors.bg },
  top: { flexDirection: 'row', justifyContent: 'space-between', alignItems: 'center', paddingHorizontal: space.gutter, paddingVertical: 10 },
  round: { width: 44, height: 44, borderRadius: 22, backgroundColor: colors.surface2, alignItems: 'center', justifyContent: 'center' },
  scroll: { paddingHorizontal: space.gutter, paddingBottom: 48 },
  hero: { padding: 20, borderRadius: radius.xl, backgroundColor: colors.surface, borderWidth: 1, marginTop: 8 },
  requirement: { flexDirection: 'row', alignItems: 'center', gap: 10, paddingTop: 14 },
  pointRow: { flexDirection: 'row', alignItems: 'center', gap: 14, paddingVertical: 11 },
  pointCard: { flexDirection: 'row', alignItems: 'center', gap: 12, marginTop: 12, padding: 12, borderRadius: radius.md, backgroundColor: colors.surface2 },
  points: { minWidth: 48, fontSize: 18, fontWeight: '800', fontVariant: ['tabular-nums'] },
  reqIcon: { width: 22, height: 22, borderRadius: 11, alignItems: 'center', justifyContent: 'center' },
  track: { height: 6, borderRadius: 3, backgroundColor: colors.surface3, overflow: 'hidden', marginTop: 4 },
  fill: { height: 6, borderRadius: 3 },
  tiles: { flexDirection: 'row', flexWrap: 'wrap', gap: 10, marginTop: 14 },
  tile: { width: '48%', flexGrow: 1, padding: 16, borderRadius: radius.lg, backgroundColor: colors.surface, borderWidth: 1, borderColor: colors.line },
  achievement: { flexDirection: 'row', alignItems: 'center', gap: 12, paddingVertical: 10 },
  medal: { width: 34, height: 34, borderRadius: 17, backgroundColor: colors.goldSoft, alignItems: 'center', justifyContent: 'center' },
}));
