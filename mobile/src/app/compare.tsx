// Compare two swings: scores, matching positions side by side, and each check. A change is only called
// an improvement when both swings measured it reliably and it is bigger than measurement noise (the
// server decides: swingguru/swing_history.py); otherwise it says "about the same" or why it can't compare.
import { useEvent } from 'expo';
import { Image } from 'expo-image';
import { router, useLocalSearchParams } from 'expo-router';
import { SymbolView } from 'expo-symbols';
import { useVideoPlayer, VideoView } from 'expo-video';
import { useEffect, useMemo, useState } from 'react';
import { ActivityIndicator, Pressable, ScrollView, StyleSheet, Text, useWindowDimensions, View } from 'react-native';

import { swingDate } from '@/components/swing-card';
import { Button, Caption, Card, EmptyState, SafeArea, StatusText } from '@/components/ui';
import { track } from '@/lib/analytics';
import { api, cachedSwing, friendly } from '@/lib/api';
import type { CompareResult, Comparison, SwingDetail, SwingListItem } from '@/lib/types';
import { useSwings } from '@/lib/use-swings';
import { colors, radius, space, type, themed } from '@/theme';

type Moment = 'address' | 'top' | 'impact';
const MOMENTS: { id: Moment; label: string }[] = [{ id: 'address', label: 'Address' }, { id: 'top', label: 'Top' }, { id: 'impact', label: 'Impact' }];

const RESULT: Record<CompareResult, { label: string; tone: 'good' | 'problem' | 'info' | 'withheld' }> = {
  improved: { label: 'Improved', tone: 'good' },
  worse: { label: 'Worse', tone: 'problem' },
  same: { label: 'About the same', tone: 'info' },
  not_comparable: { label: "Can't compare", tone: 'withheld' },
};

export default function Compare() {
  const params = useLocalSearchParams<{ a?: string; b?: string; with?: string }>();
  const [pair, setPair] = useState<[string, string] | null>(params.a && params.b ? [params.a, params.b] : null);
  return (
    <SafeArea style={styles.screen} edges={['top']}>
      <View style={styles.top}>
        <Pressable accessibilityRole="button" accessibilityLabel="Close" hitSlop={8} style={styles.round}
          onPress={() => (pair && !params.a ? setPair(null) : router.canGoBack() ? router.back() : router.replace('/'))}>
          <SymbolView name={pair && !params.a ? 'chevron.left' : 'xmark'} size={16} tintColor={colors.ink} />
        </Pressable>
        <Text style={[type.headline, { color: colors.ink }]}>Compare swings</Text>
        <View style={{ width: 44 }} />
      </View>
      {pair ? <Result a={pair[0]} b={pair[1]} /> : <Picker preselect={params.with} onCompare={setPair} />}
    </SafeArea>
  );
}

function Picker({ preselect, onCompare }: { preselect?: string; onCompare: (pair: [string, string]) => void }) {
  const { swings, error } = useSwings();
  const [picked, setPicked] = useState<string[]>(preselect ? [preselect] : []);
  const { width } = useWindowDimensions();
  const size = (width - space.gutter * 2 - 20) / 3;
  if (error) return <EmptyState icon="wifi.exclamationmark" title="Couldn't load your swings" text={error} />;
  if (!swings) return <ActivityIndicator style={{ marginTop: 40 }} color={colors.green} />;
  if (swings.length < 2) {
    return <EmptyState icon="square.split.2x1" title="Two swings needed" text="Analyse another swing, then compare them here." />;
  }
  const toggle = (id: string) => setPicked((p) => (p.includes(id) ? p.filter((x) => x !== id) : [...p.slice(-1), id]));
  const go = () => {
    const chosen = swings.filter((s) => picked.includes(s.id))
      .sort((x, y) => (x.captured_at ?? x.created_at).localeCompare(y.captured_at ?? y.created_at));
    onCompare([chosen[0].id, chosen[1].id]);
  };
  return (
    <View style={{ flex: 1 }}>
      <Text style={[type.callout, { color: colors.ink2, paddingHorizontal: space.gutter, marginBottom: 12 }]}>
        Choose two swings. We&apos;ll compare the earlier one with the later one.
      </Text>
      <ScrollView contentContainerStyle={styles.grid}>
        {swings.map((s) => <Pick key={s.id} swing={s} size={size} order={picked.indexOf(s.id)} onPress={() => toggle(s.id)} />)}
      </ScrollView>
      <View style={styles.footer}>
        <Button label={picked.length === 2 ? 'Compare' : `Choose ${2 - picked.length} more`} block disabled={picked.length !== 2} onPress={go} />
      </View>
    </View>
  );
}

function Pick({ swing, size, order, onPress }: { swing: SwingListItem; size: number; order: number; onPress: () => void }) {
  const on = order >= 0;
  return (
    <Pressable accessibilityRole="checkbox" accessibilityState={{ checked: on }} onPress={onPress}
      accessibilityLabel={`${swing.club ?? 'Swing'}, ${swingDate(swing)}`}
      style={[styles.pick, { width: size, borderColor: on ? colors.green : 'transparent' }]}>
      {swing.thumb_url ? <Image source={{ uri: swing.thumb_url }} style={{ width: '100%', aspectRatio: 3 / 4 }} contentFit="cover" /> : <View style={{ aspectRatio: 3 / 4, backgroundColor: colors.surface2 }} />}
      <View style={{ padding: 8 }}>
        <Text numberOfLines={1} style={[type.footnote, { color: colors.ink, fontWeight: '600' }]}>{swing.club ?? 'Swing'}</Text>
        <Text numberOfLines={1} style={[type.footnote, { color: colors.ink3 }]}>{swingDate(swing)}</Text>
      </View>
      {swing.score?.total != null ? <Text style={styles.pickScore}>{swing.score.total.toFixed(1)}</Text> : null}
      {on ? <View style={styles.tick}><SymbolView name="checkmark" size={12} tintColor={colors.onAccent} weight="bold" /></View> : null}
    </Pressable>
  );
}

function Result({ a, b }: { a: string; b: string }) {
  const [data, setData] = useState<Comparison | null>(null);
  const [details, setDetails] = useState<[SwingDetail | null, SwingDetail | null]>([cachedSwing(a), cachedSwing(b)]);
  const [error, setError] = useState<string | null>(null);
  const [moment, setMoment] = useState<Moment>('address');
  useEffect(() => {
    api.compare(a, b).then((c) => {
      setData(c);
      track('comparison_used', { same_club: c.swings.a.club === c.swings.b.club, comparable_checks: 8 - c.counts.not_comparable });
    }).catch((e) => setError(friendly(e)));
    Promise.all([api.swing(a), api.swing(b)]).then(([x, y]) => setDetails([x, y])).catch(() => undefined);
  }, [a, b]);
  if (error) return <EmptyState icon="wifi.exclamationmark" title="Couldn't compare these swings" text={error} />;
  if (!data) return <ActivityIndicator style={{ marginTop: 40 }} color={colors.green} />;
  const c = data.counts;
  const summary = [c.improved ? `${c.improved} improved` : null, c.worse ? `${c.worse} worse` : null,
    c.same ? `${c.same} about the same` : null].filter(Boolean).join(' · ');
  return (
    <ScrollView contentContainerStyle={styles.scroll} showsVerticalScrollIndicator={false}>
      {data.duplicate ? (
        <View style={styles.note}><SymbolView name="exclamationmark.circle" size={16} tintColor={colors.attentionInk} />
          <Text style={[type.footnote, { color: colors.ink2, flex: 1 }]}>These look like the same swing uploaded twice, so any difference is just measurement noise.</Text></View>
      ) : null}
      <ScoreCompare data={data} />

      <Caption style={{ marginTop: 24, marginBottom: 10 }}>Positions</Caption>
      <View style={styles.segment}>
        {MOMENTS.map((m) => (
          <Pressable key={m.id} accessibilityRole="tab" accessibilityState={{ selected: moment === m.id }} onPress={() => setMoment(m.id)}
            style={[styles.segmentItem, moment === m.id && { backgroundColor: colors.surface }]}>
            <Text style={[type.subhead, { color: moment === m.id ? colors.ink : colors.ink2, fontWeight: '600' }]}>{m.label}</Text>
          </Pressable>
        ))}
      </View>
      <View style={{ flexDirection: 'row', gap: 10, marginTop: 10 }}>
        {(['a', 'b'] as const).map((k, i) => (
          <Frame key={k} detail={details[i]} time={data.moments?.[k]?.[moment]} label={i === 0 ? 'Earlier' : 'Later'} />
        ))}
      </View>

      <Caption style={{ marginTop: 26, marginBottom: 6 }}>Checks</Caption>
      {summary ? <Text style={[type.callout, { color: colors.ink, marginBottom: 12 }]}>{summary}</Text> : null}
      {data.rows.map((row) => (
        <View key={row.id} style={[styles.row, row.result === 'not_comparable' && { opacity: 0.7 }]}>
          <View style={{ flexDirection: 'row', alignItems: 'center', justifyContent: 'space-between', gap: 10 }}>
            <Text style={[type.headline, { color: colors.ink, flexShrink: 1 }]}>{row.name} <Text style={{ color: colors.ink3, fontWeight: '500' }}>· {row.phase_label}</Text></Text>
            <StatusText label={RESULT[row.result].label} status={RESULT[row.result].tone} />
          </View>
          {row.a && row.b ? (
            <View style={{ flexDirection: 'row', alignItems: 'center', gap: 8, marginTop: 8 }}>
              <StatusText label={row.a.label} status={row.a.status} />
              <SymbolView name="arrow.right" size={11} tintColor={colors.ink3} />
              <StatusText label={row.b.label} status={row.b.status} />
            </View>
          ) : null}
          <Text style={[type.footnote, { color: colors.ink3, marginTop: 6 }]}>{row.reason}</Text>
        </View>
      ))}
      {data.notes.map((n) => <Text key={n} style={[type.footnote, { color: colors.ink3, marginTop: 8 }]}>{n}</Text>)}
    </ScrollView>
  );
}

function ScoreCompare({ data }: { data: Comparison }) {
  const s = data.score;
  const tone = s.result === 'improved' ? colors.good : s.result === 'worse' ? colors.problem : colors.ink3;
  return (
    <Card>
      <Caption>Swing Score</Caption>
      <View style={{ flexDirection: 'row', alignItems: 'center', marginTop: 10 }}>
        {(['a', 'b'] as const).map((k, i) => (
          <View key={k} style={{ flex: 1, alignItems: 'center' }}>
            <Text style={[type.number, { fontSize: 40, color: i === 1 ? colors.ink : colors.ink2 }]}>{s[k] != null ? (s[k] as number).toFixed(1) : '–'}</Text>
            <Text style={[type.footnote, { color: colors.ink3 }]}>{data.swings[k].club ?? 'Swing'} · {new Date(data.swings[k].created_at).toLocaleDateString('en-GB', { day: 'numeric', month: 'short' })}</Text>
          </View>
        ))}
      </View>
      <View style={[styles.delta, { borderColor: tone }]}>
        <Text style={[type.subhead, { color: tone, fontWeight: '700' }]}>
          {s.result === 'not_comparable' ? s.reason : s.delta === null ? '' :
            s.result === 'same' ? 'About the same' : `${s.delta > 0 ? '+' : ''}${s.delta.toFixed(1)} ${s.result === 'improved' ? 'better' : 'lower'}`}
        </Text>
      </View>
    </Card>
  );
}

/** One swing's frame at a moment: its replay video, paused at the analysis' moment time. */
function Frame({ detail, time, label }: { detail: SwingDetail | null; time?: number; label: string }) {
  const source = useMemo(() => (detail ? { uri: detail.replay.video_url } : null), [detail]);
  const player = useVideoPlayer(source, (p) => {
    p.muted = true;
    p.pause();
  });
  // Seek once the video can play (a seek before it has loaded is dropped), and again on a new moment.
  const { status } = useEvent(player, 'statusChange', { status: player.status });
  useEffect(() => {
    // seekBy (relative) rather than assigning currentTime: the player belongs to the hook.
    if (time !== undefined && status === 'readyToPlay') player.seekBy(time - player.currentTime);
  }, [player, time, source, status]);
  return (
    <View style={{ flex: 1 }}>
      <View style={styles.frame}>
        {detail && time !== undefined ? (
          <VideoView player={player} style={StyleSheet.absoluteFill} contentFit="contain" nativeControls={false} />
        ) : (
          <View style={{ flex: 1, alignItems: 'center', justifyContent: 'center', padding: 10 }}>
            {detail ? <Text style={[type.footnote, { color: colors.ink3, textAlign: 'center' }]}>This position wasn&apos;t found on this swing</Text>
              : <ActivityIndicator color={colors.green} />}
          </View>
        )}
      </View>
      <Text style={[type.footnote, { color: colors.ink3, textAlign: 'center', marginTop: 6 }]}>{label}</Text>
    </View>
  );
}

const styles = themed(() => StyleSheet.create({
  screen: { flex: 1, backgroundColor: colors.bg },
  top: { flexDirection: 'row', justifyContent: 'space-between', alignItems: 'center', paddingHorizontal: space.gutter, paddingVertical: 10 },
  round: { width: 44, height: 44, borderRadius: 22, backgroundColor: colors.surface2, alignItems: 'center', justifyContent: 'center' },
  scroll: { paddingHorizontal: space.gutter, paddingBottom: 48 },
  grid: { flexDirection: 'row', flexWrap: 'wrap', gap: 10, paddingHorizontal: space.gutter, paddingBottom: 120 },
  pick: { borderRadius: radius.md, overflow: 'hidden', backgroundColor: colors.surface, borderWidth: 2 },
  pickScore: { position: 'absolute', top: 6, left: 6, color: '#fff', fontWeight: '800', fontSize: 13, backgroundColor: 'rgba(0,0,0,0.55)', paddingHorizontal: 7, paddingVertical: 2, borderRadius: 8, overflow: 'hidden' },
  tick: { position: 'absolute', top: 6, right: 6, width: 24, height: 24, borderRadius: 12, backgroundColor: colors.green, alignItems: 'center', justifyContent: 'center' },
  footer: { position: 'absolute', left: 0, right: 0, bottom: 0, padding: space.gutter, paddingBottom: 34, backgroundColor: colors.bg, borderTopWidth: StyleSheet.hairlineWidth, borderTopColor: colors.line },
  note: { flexDirection: 'row', gap: 8, alignItems: 'center', padding: 12, borderRadius: radius.md, backgroundColor: colors.attentionSoft, marginBottom: 12 },
  delta: { alignSelf: 'center', marginTop: 14, paddingHorizontal: 14, paddingVertical: 6, borderRadius: radius.pill, borderWidth: 1 },
  segment: { flexDirection: 'row', padding: 3, borderRadius: radius.md, backgroundColor: colors.surface2 },
  segmentItem: { flex: 1, alignItems: 'center', paddingVertical: 9, borderRadius: radius.sm },
  frame: { aspectRatio: 9 / 14, borderRadius: radius.md, overflow: 'hidden', backgroundColor: '#000' },
  row: { padding: 16, borderRadius: radius.md, backgroundColor: colors.surface, borderWidth: 1, borderColor: colors.line, marginBottom: 8 },
}));
