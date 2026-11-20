// Weekly league: this week's group of 20, your league points (LP) and what earns them, last week's result.
// Everything comes from GET /v1/league; practice rivals are always labelled as simulated.
import { router, useFocusEffect } from 'expo-router';
import { SymbolView } from 'expo-symbols';
import { useCallback, useEffect, useRef, useState } from 'react';
import {
  ActivityIndicator, Alert, KeyboardAvoidingView, Platform, Pressable, RefreshControl, ScrollView, StyleSheet, Text,
  View,
} from 'react-native';
import Animated, { FadeIn, useReducedMotion } from 'react-native-reanimated';

import { TIER_ACCENT } from '@/components/rank-badge';
import { Button, Caption, EmptyState, SafeArea, Section } from '@/components/ui';
import { track } from '@/lib/analytics';
import { api, ApiError, friendly } from '@/lib/api';
import { rememberPosition } from '@/lib/league-seen';
import {
  avatarSlot, breakdown, initialsOf, insights, memberLabel, movement, ordinal, outcomeText, rivalCountText, rulesLines,
  timeLeft, zoneLines,
} from '@/lib/league-view';
import type { League, LeagueMember } from '@/lib/types';
import { colors, radius, space, type, themed } from '@/theme';

export default function LeagueScreen() {
  const [data, setData] = useState<League | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [stale, setStale] = useState(false);          // a refresh failed but the last standings are still shown
  const [refreshing, setRefreshing] = useState(false);
  const [unavailable, setUnavailable] = useState(false);
  const [moved, setMoved] = useState(0);
  const loaded = useRef(false);
  // Refreshes never blank the screen: the current table stays until the new one arrives (no flicker).
  const load = useCallback((quiet = false) => {
    if (!quiet && loaded.current) setRefreshing(true);
    return api.league().then((l) => {
      setError(null);
      setStale(false);
      setData(l);
      if (l.you) setMoved(movement(rememberPosition(l.week.start, l.you.position), l.you.position));
      if (!loaded.current) track('league_viewed', { joined: l.joined, division: l.profile?.division ?? null });
      loaded.current = true;
    }).catch((e) => {
      if (e instanceof ApiError && e.status === 404) setUnavailable(true);
      else if (loaded.current) setStale(true);
      else setError(friendly(e));
    }).finally(() => setRefreshing(false));
  }, []);
  useFocusEffect(useCallback(() => { void load(true); }, [load]));

  return (
    <SafeArea style={styles.screen} edges={['top']}>
      <View style={styles.top}>
        <Pressable accessibilityRole="button" accessibilityLabel="Close" hitSlop={8} style={styles.round}
          onPress={() => (router.canGoBack() ? router.back() : router.replace('/'))}>
          <SymbolView name="xmark" size={16} tintColor={colors.ink} />
        </Pressable>
        <Text style={[type.headline, { color: colors.ink }]}>Weekly league</Text>
        <View style={{ width: 44 }} />
      </View>
      {unavailable ? (
        <EmptyState icon="trophy" title="Leagues are coming soon" text="Weekly leagues aren't available on this server yet." />
      ) : error ? (
        <EmptyState icon="wifi.exclamationmark" title="Couldn't load the league" text={error} action={<Button label="Try again" onPress={() => void load()} />} />
      ) : !data ? (
        <View style={{ flex: 1, alignItems: 'center', justifyContent: 'center', gap: 10 }} accessibilityLabel="Loading the league">
          <ActivityIndicator color={colors.green} />
          <Text style={[type.footnote, { color: colors.ink3 }]}>Loading this week&apos;s league…</Text>
        </View>
      ) : !data.joined ? (
        <Join league={data} onJoined={setData} />
      ) : (
        <ScrollView contentContainerStyle={styles.scroll} showsVerticalScrollIndicator={false}
          refreshControl={<RefreshControl refreshing={refreshing} onRefresh={() => void load()} tintColor={colors.green} />}>
          {stale ? (
            <Text style={[type.footnote, styles.stale]} accessibilityLiveRegion="polite">
              Couldn&apos;t refresh just now. Showing the latest standings; pull down to try again.
            </Text>
          ) : null}
          {data.last_result ? <LastResult league={data} /> : null}
          <Hero league={data} />
          <Table league={data} moved={moved} />
          <YourWeek league={data} />
          <HowItWorks league={data} />
          <Settings league={data} onChanged={setData} onLeft={() => void load()} />
        </ScrollView>
      )}
    </SafeArea>
  );
}

function Join({ league, onJoined }: { league: League; onJoined: (l: League) => void }) {
  const [name, setName] = useState('');
  const [busy, setBusy] = useState(false);
  const [problem, setProblem] = useState<string | null>(null);
  const need = Math.max(0, (league.join_min_swings ?? 5) - (league.counted_swings ?? 0));
  const join = () => {
    setBusy(true);
    setProblem(null);
    api.leagueJoin(name.trim()).then((l) => {
      track('league_joined', { division: l.profile?.division ?? null });
      onJoined(l);
    }).catch((e) => setProblem(friendly(e))).finally(() => setBusy(false));
  };
  return (
    <KeyboardAvoidingView behavior={Platform.OS === 'ios' ? 'padding' : undefined} style={{ flex: 1 }}>
      <ScrollView contentContainerStyle={styles.scroll} keyboardShouldPersistTaps="handled">
        <View style={[styles.hero, { borderColor: colors.goldLine }]}>
          <View style={styles.trophy}><SymbolView name="trophy.fill" size={30} tintColor={colors.gold} /></View>
          <Text style={[type.title2, { color: colors.ink, marginTop: 14 }]}>Compete every week</Text>
          <Text style={[type.callout, { color: colors.ink2, marginTop: 6 }]}>
            Join a group of {league.rules.group_size}. Practise, improve and fix checks to earn league points. The top players
            move up a division every Monday. Everyone starts in Bronze.
          </Text>
          <View style={{ marginTop: 14, gap: 8 }}>
            {['Points come from practice and genuine improvement, not from uploading more.',
              'Some places are filled by practice rivals. They are simulated, always labelled, and play by the same rules.',
              'Other players only see your league name and points. Never your swings or scores.'].map((t) => (
              <View key={t} style={{ flexDirection: 'row', gap: 10 }}>
                <SymbolView name="checkmark.circle.fill" size={16} tintColor={colors.green} />
                <Text style={[type.footnote, { color: colors.ink2, flex: 1 }]}>{t}</Text>
              </View>
            ))}
          </View>
        </View>
        {need > 0 ? (
          <View style={[styles.card, { marginTop: 16 }]}>
            <Text style={[type.headline, { color: colors.ink }]}>Analyse {need} more {need === 1 ? 'swing' : 'swings'} to join</Text>
            <Text style={[type.footnote, { color: colors.ink2, marginTop: 4 }]}>
              The league opens once your provisional rank is confirmed ({league.join_min_swings} counted swings).
            </Text>
            <View style={{ marginTop: 14 }}><Button label="Analyse a swing" icon="video.fill" onPress={() => router.push('/analyze')} /></View>
          </View>
        ) : (
          <View style={[styles.card, { marginTop: 16 }]}>
            <Text style={[type.headline, { color: colors.ink }]}>Choose your league name</Text>
            <Text style={[type.footnote, { color: colors.ink2, marginTop: 4 }]}>Pick one of these. It&apos;s shown to the others in your group.</Text>
            <NamePicker value={name} onChange={setName} />
            {problem ? <Text style={[type.footnote, { color: colors.problemInk, marginTop: 8 }]}>{problem}</Text> : null}
            <View style={{ marginTop: 14 }}>
              <Button label="Join the league" icon="trophy.fill" block busy={busy} disabled={!name} onPress={join} />
            </View>
          </View>
        )}
      </ScrollView>
    </KeyboardAvoidingView>
  );
}

/** League names are chosen from server-generated suggestions (no free text: nothing user-written is shown to others). */
function NamePicker({ value, onChange }: { value: string; onChange: (name: string) => void }) {
  const [page, setPage] = useState(0);
  const [names, setNames] = useState<string[] | null>(null);
  const [failed, setFailed] = useState(false);
  useEffect(() => {
    let alive = true;
    api.leagueNames(page).then((n) => { if (alive) setNames(n); }).catch(() => { if (alive) setFailed(true); });
    return () => { alive = false; };
  }, [page]);
  return (
    <View style={{ marginTop: 12 }}>
      {failed ? (
        <Text style={[type.footnote, { color: colors.problemInk }]}>Couldn&apos;t load names. Tap &quot;More names&quot; to try again.</Text>
      ) : !names ? (
        <ActivityIndicator color={colors.green} style={{ alignSelf: 'flex-start' }} />
      ) : (
        <View style={styles.names}>
          {names.map((n) => {
            const selected = n === value;
            return (
              <Pressable key={n} accessibilityRole="radio" accessibilityState={{ selected }} onPress={() => onChange(n)}
                style={[styles.nameChip, selected && styles.nameChipOn]}>
                <Text style={[type.subhead, { color: selected ? colors.onAccent : colors.ink, fontWeight: '600' }]}>{n}</Text>
              </Pressable>
            );
          })}
        </View>
      )}
      <View style={{ marginTop: 8, alignSelf: 'flex-start' }}>
        <Button label="More names" kind="quiet" small icon="arrow.clockwise" onPress={() => { onChange(''); setNames(null); setFailed(false); setPage((p) => p + 1); }} />
      </View>
    </View>
  );
}

function LastResult({ league }: { league: League }) {
  const r = league.last_result!;
  const up = r.outcome === 'promoted' || r.outcome === 'medal';
  const down = r.outcome === 'relegated';
  return (
    <View style={[styles.result, { backgroundColor: up ? colors.goodSoft : down ? colors.attentionSoft : colors.surface2 }]}
      accessibilityLabel={`Last week: ${ordinal(r.position)} in ${r.division} with ${r.lp} LP. ${outcomeText(r)}`}>
      <SymbolView name={up ? 'arrow.up.circle.fill' : down ? 'arrow.down.circle.fill' : 'equal.circle.fill'} size={26}
        tintColor={up ? colors.goodInk : down ? colors.attentionInk : colors.ink2} />
      <View style={{ flex: 1 }}>
        <Text style={[type.headline, { color: colors.ink }]}>{outcomeText(r)}</Text>
        <Text style={[type.footnote, { color: colors.ink2, marginTop: 2 }]}>Last week: {ordinal(r.position)} in {r.division} · {r.lp} LP</Text>
      </View>
    </View>
  );
}

function Hero({ league }: { league: League }) {
  const g = league.group!;
  const you = league.you;
  const accent = TIER_ACCENT[g.division];
  const lines = insights(league);
  return (
    <View style={[styles.hero, { borderColor: colors.goldLine }]}>
      <View style={{ flexDirection: 'row', alignItems: 'center', gap: 14 }}>
        <View style={[styles.trophy, { backgroundColor: colors.surface2 }]}><SymbolView name="trophy.fill" size={28} tintColor={accent} /></View>
        <View style={{ flex: 1 }}>
          <Caption style={{ color: colors.gold }}>{g.division} league · {timeLeft(league.week.end)}</Caption>
          <Text style={[type.title1, { color: colors.ink }]}>{you ? ordinal(you.position) : '–'}</Text>
          <Text style={[type.footnote, { color: colors.ink2 }]}>{you ? `${you.lp} LP this week${you.pending ? ` · +${you.pending} pending at the reset` : ''}` : ''}</Text>
        </View>
      </View>
      {lines.map((l, i) => (
        <Text key={l} style={[i === 0 ? type.callout : type.footnote, { color: i === 0 ? colors.ink : colors.ink2, marginTop: i === 0 ? 14 : 4 }]}>{l}</Text>
      ))}
      <Text style={[type.footnote, { color: colors.ink3, marginTop: 8 }]}>
        Top {g.promote} {g.division === 'Diamond' ? 'win a Diamond medal' : 'move up'}{g.relegate ? `, bottom ${g.relegate} move down` : ''}.
        Resets {new Date(league.week.end).toLocaleString('en-GB', { weekday: 'short', hour: '2-digit', minute: '2-digit' })}.
      </Text>
    </View>
  );
}

// Muted avatar colours that read well on both light and dark backgrounds (white initials on top).
const AVATAR_COLOURS = ['#4f7a63', '#5b6fa3', '#a0674f', '#7d5f9e', '#3f7f88', '#9a7a35', '#a35b73', '#5f7f3f'];
const MEDALS = ['#d4af37', '#a8b0b8', '#b87333'];

function Avatar({ name, rival }: { name: string; rival: boolean }) {
  return (
    // Practice rivals have a dashed outer ring: a quiet, consistent marker (explained under the table).
    <View style={[styles.avatarRing, rival && styles.avatarRingRival]}>
      <View style={[styles.avatar, { backgroundColor: AVATAR_COLOURS[avatarSlot(name) % AVATAR_COLOURS.length] }]}>
        <Text style={styles.initials} allowFontScaling={false}>{initialsOf(name)}</Text>
      </View>
    </View>
  );
}

function Table({ league, moved }: { league: League; moved: number }) {
  const g = league.group!;
  const { promoteAfter, relegateAfter } = zoneLines(league);
  const reduced = useReducedMotion();
  const showRival = (m: LeagueMember) => Alert.alert(m.name, league.rival_profile_line);
  return (
    <Section title="This week">
      <View style={styles.table}>
        {g.members.map((m) => {
          const medal = m.position <= 3 && m.lp > 0 ? MEDALS[m.position - 1] : null;
          return (
            <View key={`${m.is_rival ? 'r' : 'u'}:${m.name}`}>
              <Pressable disabled={!m.is_rival} onPress={() => showRival(m)} accessibilityLabel={memberLabel(m, league.rival_label)}
                accessibilityHint={m.is_rival ? 'Shows what a practice rival is' : undefined}
                style={({ pressed }) => [styles.row, m.is_you && styles.youRow, pressed && m.is_rival && { opacity: 0.7 }]}>
                {medal ? (
                  <View style={[styles.medal, { backgroundColor: medal }]}><Text style={styles.medalText} allowFontScaling={false}>{m.position}</Text></View>
                ) : (
                  <Text style={[styles.pos, { color: m.zone === 'promote' || m.zone === 'medal' ? colors.goodInk : m.zone === 'relegate' ? colors.problemInk : colors.ink3 }]}>
                    {m.position}
                  </Text>
                )}
                <Avatar name={m.name} rival={m.is_rival} />
                <Text style={[type.subhead, { color: colors.ink, flex: 1, fontWeight: m.is_you ? '700' : '500' }]} numberOfLines={1}>
                  {m.name}{m.is_you ? ' (you)' : ''}
                </Text>
                {m.is_you && moved !== 0 ? (
                  <Animated.View entering={reduced ? undefined : FadeIn.duration(400)}
                    style={[styles.moved, { backgroundColor: moved > 0 ? colors.goodSoft : colors.attentionSoft }]}>
                    <Text style={{ fontSize: 11, fontWeight: '700', color: moved > 0 ? colors.goodInk : colors.attentionInk }}>
                      {moved > 0 ? `▲${moved}` : `▼${-moved}`}
                    </Text>
                  </Animated.View>
                ) : null}
                <Text style={[styles.lp, m.lp === 0 && { color: colors.ink3 }]}>{m.lp}</Text>
              </Pressable>
              {m.position === promoteAfter ? (
                <ZoneLine label={g.division === 'Diamond' ? 'Medal places above' : 'Promotion zone above'} color={colors.good} />
              ) : null}
              {m.position === relegateAfter ? <ZoneLine label="Relegation zone below" color={colors.problem} /> : null}
            </View>
          );
        })}
      </View>
      {g.rival_count ? (
        <Pressable accessibilityRole="button" onPress={() => Alert.alert('Practice rivals', league.rival_profile_line)}
          style={styles.key} hitSlop={6}>
          <View style={[styles.keyRing]} />
          <Text style={[type.footnote, { color: colors.ink3, flex: 1 }]}>
            {`Dashed ring: practice rival (computer-generated) · ${rivalCountText(g.rival_count)}`}
          </Text>
          <SymbolView name="info.circle" size={14} tintColor={colors.ink3} />
        </Pressable>
      ) : null}
    </Section>
  );
}

function ZoneLine({ label, color }: { label: string; color: string }) {
  return (
    <View style={styles.zone} accessibilityElementsHidden>
      <View style={[styles.zoneRule, { backgroundColor: color }]} />
      <Text style={[type.caption, { color, fontSize: 10 }]}>{label}</Text>
      <View style={[styles.zoneRule, { backgroundColor: color }]} />
    </View>
  );
}

function YourWeek({ league }: { league: League }) {
  const rows = breakdown(league);
  if (!rows.length) return null;
  return (
    <Section title="Your league points">
      <View style={styles.card}>
        {rows.map((r, i) => (
          <View key={r.source} style={[styles.breakRow, i > 0 && { borderTopWidth: StyleSheet.hairlineWidth, borderTopColor: colors.line }]}
            accessibilityLabel={`${r.label}: ${r.lp} of ${r.max} LP. ${r.detail}`}>
            <View style={{ flex: 1 }}>
              <Text style={[type.subhead, { color: colors.ink }]}>{r.label}</Text>
              <Text style={[type.footnote, { color: colors.ink3, marginTop: 2 }]}>{r.detail}</Text>
            </View>
            <Text style={[type.subhead, { color: r.lp ? colors.goodInk : colors.ink3, fontWeight: '700', fontVariant: ['tabular-nums'] }]}>
              {r.lp}<Text style={{ color: colors.ink3, fontWeight: '500' }}> / {r.max}</Text>
            </Text>
          </View>
        ))}
        {league.you?.baseline_default ? (
          <Text style={[type.footnote, { color: colors.ink3, marginTop: 10 }]}>
            Your recent level uses the {league.group!.division} starting value until you have 5 eligible swings from earlier weeks.
          </Text>
        ) : null}
      </View>
    </Section>
  );
}

function HowItWorks({ league }: { league: League }) {
  const [open, setOpen] = useState(false);
  return (
    <Section title="How league points work" action={<Button label={open ? 'Hide' : 'Show'} kind="quiet" small onPress={() => setOpen(!open)} />}>
      {open ? (
        <View style={[styles.card, { gap: 10 }]}>
          {rulesLines(league).map((t) => (
            <View key={t} style={{ flexDirection: 'row', gap: 10 }}>
              <Text style={{ color: colors.green }}>•</Text>
              <Text style={[type.footnote, { color: colors.ink2, flex: 1 }]}>{t}</Text>
            </View>
          ))}
        </View>
      ) : null}
    </Section>
  );
}

function Settings({ league, onChanged, onLeft }: { league: League; onChanged: (l: League) => void; onLeft: () => void }) {
  const [editing, setEditing] = useState(false);
  const [name, setName] = useState('');
  const [problem, setProblem] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const save = () => {
    setBusy(true);
    setProblem(null);
    api.leagueRename(name).then((l) => { onChanged(l); setEditing(false); setName(''); })
      .catch((e) => setProblem(friendly(e))).finally(() => setBusy(false));
  };
  const leave = () => Alert.alert('Leave the league?', 'Your league name, results and this week\'s points are deleted. You can join again later, starting in Bronze.', [
    { text: 'Cancel', style: 'cancel' },
    { text: 'Leave', style: 'destructive', onPress: () => { api.leagueLeave().then(onLeft).catch((e) => Alert.alert('Couldn\'t leave', friendly(e))); } },
  ]);
  return (
    <Section title="League name">
      <View style={styles.card}>
        {editing ? (
          <>
            <Text style={[type.footnote, { color: colors.ink2 }]}>Pick a new name:</Text>
            <NamePicker value={name} onChange={setName} />
            {problem ? <Text style={[type.footnote, { color: colors.problemInk, marginTop: 8 }]}>{problem}</Text> : null}
            <View style={{ flexDirection: 'row', gap: 10, marginTop: 12 }}>
              <Button label="Save" small busy={busy} disabled={!name} onPress={save} />
              <Button label="Cancel" kind="quiet" small onPress={() => { setEditing(false); setName(''); }} />
            </View>
          </>
        ) : (
          <View style={{ flexDirection: 'row', alignItems: 'center' }}>
            <Text style={[type.headline, { color: colors.ink, flex: 1 }]}>{league.profile?.display_name}</Text>
            <Button label="Change" kind="quiet" small onPress={() => setEditing(true)} />
          </View>
        )}
      </View>
      <View style={{ marginTop: 12, alignItems: 'center' }}>
        <Button label="Leave the league" kind="danger" small onPress={leave} />
      </View>
      <Text style={[type.footnote, { color: colors.ink3, textAlign: 'center', marginTop: 16 }]}>
        {"League points measure practice and improvement in your analysed swings. They're not a handicap."}
      </Text>
    </Section>
  );
}

const styles = themed(() => StyleSheet.create({
  screen: { flex: 1, backgroundColor: colors.bg },
  top: { flexDirection: 'row', justifyContent: 'space-between', alignItems: 'center', paddingHorizontal: space.gutter, paddingVertical: 10 },
  round: { width: 44, height: 44, borderRadius: 22, backgroundColor: colors.surface2, alignItems: 'center', justifyContent: 'center' },
  scroll: { paddingHorizontal: space.gutter, paddingBottom: 48 },
  hero: { padding: 20, borderRadius: radius.xl, backgroundColor: colors.surface, borderWidth: 1, marginTop: 8 },
  trophy: { width: 60, height: 60, borderRadius: 30, backgroundColor: colors.goldSoft, alignItems: 'center', justifyContent: 'center' },
  card: { padding: 16, borderRadius: radius.lg, backgroundColor: colors.surface, borderWidth: 1, borderColor: colors.line },
  result: { flexDirection: 'row', alignItems: 'center', gap: 12, padding: 14, borderRadius: radius.lg, marginTop: 8 },
  names: { flexDirection: 'row', flexWrap: 'wrap', gap: 8 },
  nameChip: { minHeight: 40, justifyContent: 'center', paddingHorizontal: 14, borderRadius: radius.pill, borderWidth: 1,
              borderColor: colors.lineStrong, backgroundColor: colors.surface2 },
  nameChipOn: { backgroundColor: colors.green, borderColor: colors.green },
  table: { borderRadius: radius.lg, backgroundColor: colors.surface, borderWidth: 1, borderColor: colors.line, overflow: 'hidden' },
  // Compact rows: 44 pt (Apple's minimum tap height), so about 12 players fit on a small iPhone screen.
  row: { flexDirection: 'row', alignItems: 'center', gap: 10, paddingHorizontal: 12, minHeight: 44, paddingVertical: 3 },
  youRow: { backgroundColor: colors.greenSoft, borderLeftWidth: 3, borderLeftColor: colors.green, paddingLeft: 9 },
  pos: { width: 22, textAlign: 'center', fontSize: 14, fontWeight: '700', fontVariant: ['tabular-nums'] },
  medal: { width: 22, height: 22, borderRadius: 11, alignItems: 'center', justifyContent: 'center' },
  medalText: { color: '#1b1b1b', fontSize: 12, fontWeight: '800' },
  avatarRing: { width: 32, height: 32, borderRadius: 16, alignItems: 'center', justifyContent: 'center', borderWidth: 1.5, borderColor: 'transparent' },
  avatarRingRival: { borderColor: colors.ink3, borderStyle: 'dashed' },
  avatar: { width: 26, height: 26, borderRadius: 13, alignItems: 'center', justifyContent: 'center' },
  initials: { color: '#fff', fontSize: 11, fontWeight: '700', letterSpacing: 0.3 },
  lp: { minWidth: 34, textAlign: 'right', fontSize: 15, fontWeight: '700', color: colors.ink, fontVariant: ['tabular-nums'] },
  moved: { paddingHorizontal: 6, paddingVertical: 2, borderRadius: radius.pill },
  key: { flexDirection: 'row', alignItems: 'center', gap: 8, marginTop: 10, paddingHorizontal: 4 },
  keyRing: { width: 14, height: 14, borderRadius: 7, borderWidth: 1.5, borderColor: colors.ink3, borderStyle: 'dashed' },
  stale: { color: colors.attentionInk, backgroundColor: colors.attentionSoft, padding: 10, borderRadius: radius.md, marginTop: 8 },
  zone: { flexDirection: 'row', alignItems: 'center', gap: 8, paddingHorizontal: 12, paddingVertical: 2 },
  zoneRule: { flex: 1, height: 1, opacity: 0.6 },
  breakRow: { flexDirection: 'row', alignItems: 'center', paddingVertical: 10, gap: 12 },
}));
