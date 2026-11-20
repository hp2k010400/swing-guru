// One drill: what it fixes, why it helps, the steps, reps, and how your own swings look on the checks
// it trains. A video appears for an original demonstration Swing Guru owns, or for an approved
// externally hosted one in its official player (creator credited); otherwise the written instructions
// stand on their own (no placeholders, no claim of professional review).
import { router, useLocalSearchParams } from 'expo-router';
import { SymbolView } from 'expo-symbols';
import { useVideoPlayer, VideoView } from 'expo-video';
import { useEffect, useState } from 'react';
import { ActivityIndicator, Pressable, ScrollView, StyleSheet, Text, View } from 'react-native';

import { DrillVideo } from '@/components/drill-video';
import { Caption, Card, EmptyState, SafeArea, StatusText } from '@/components/ui';
import { track } from '@/lib/analytics';
import { api, friendly } from '@/lib/api';
import { markDrillSeen } from '@/lib/drill-seen';
import { drillProps } from '@/lib/drill-video';
import type { Drill, Progress } from '@/lib/types';
import { colors, radius, space, type, themed } from '@/theme';

export default function DrillScreen() {
  const { id, from } = useLocalSearchParams<{ id: string; from?: string }>();
  const [drill, setDrill] = useState<Drill | null>(null);
  const [progress, setProgress] = useState<Progress | null>(null);
  const [error, setError] = useState<string | null>(null);
  useEffect(() => {
    api.drills().then((d) => {
      const found = d.groups.flatMap((g) => g.drills).find((x) => x.id === id) ?? null;
      setDrill(found);
      if (!found) setError("This drill isn't available.");
      else {
        const props = { ...drillProps(found), from_finding: from === 'finding' };
        track('drill_started', props); // the drill page was opened (there is no separate "start" button)
        if (markDrillSeen(found.id)) track('drill_revisited', props);
      }
    }).catch((e) => setError(friendly(e)));
    api.progress().then(setProgress).catch(() => undefined);
  }, [id, from]);

  return (
    <SafeArea style={styles.screen} edges={['top']}>
      <View style={styles.top}>
        <Pressable accessibilityRole="button" accessibilityLabel="Back" hitSlop={8} style={styles.round}
          onPress={() => (router.canGoBack() ? router.back() : router.replace('/drills'))}>
          <SymbolView name="chevron.left" size={16} tintColor={colors.ink} />
        </Pressable>
      </View>
      {error ? <EmptyState icon="target" title="Drill unavailable" text={error} /> : !drill ? (
        <ActivityIndicator style={{ marginTop: 40 }} color={colors.green} />
      ) : (
        <ScrollView contentContainerStyle={styles.scroll} showsVerticalScrollIndicator={false}>
          <Caption style={{ color: colors.greenInk }}>{drill.phase_label} · {drill.minutes} min</Caption>
          <Text style={[type.large, { color: colors.ink, marginTop: 6 }]}>{drill.name}</Text>
          {drill.targets?.length ? (
            <View style={{ flexDirection: 'row', flexWrap: 'wrap', gap: 8, marginTop: 12 }}>
              {drill.targets.map((t) => (
                <View key={t.id} style={styles.chip}><Text style={[type.footnote, { color: colors.greenInk, fontWeight: '600' }]}>Fixes: {t.name} · {t.phase_label}</Text></View>
              ))}
            </View>
          ) : null}

          {drill.video ? <OwnedVideo uri={drill.video} /> : drill.demo_video ? <DrillVideo video={drill.demo_video} drill={drill} /> : null}

          <Card style={{ marginTop: 18 }}>
            <Caption>Why it helps</Caption>
            <Text style={[type.body, { color: colors.ink, marginTop: 8 }]}>{drill.why}</Text>
          </Card>

          <Caption style={{ marginTop: 24, marginBottom: 6 }}>How to do it</Caption>
          {drill.steps.map((step, i) => (
            <View key={step} style={styles.step}>
              <View style={styles.stepNo}><Text style={{ color: colors.onAccent, fontWeight: '800', fontSize: 14 }}>{i + 1}</Text></View>
              <Text style={[type.body, { color: colors.ink, flex: 1 }]}>{step}</Text>
            </View>
          ))}
          <View style={styles.reps}>
            <SymbolView name="repeat" size={16} tintColor={colors.greenInk} />
            <Text style={[type.callout, { color: colors.ink, flex: 1 }]}>{drill.reps}</Text>
          </View>

          <YourSwings drill={drill} progress={progress} />

          <Text style={[type.footnote, { color: colors.ink3, marginTop: 22 }]}>
            {drill.status === 'reviewed' ? 'Reviewed by a golf professional.' : 'Written guidance from Swing Guru. Not yet reviewed by a golf professional.'}
          </Text>
        </ScrollView>
      )}
    </SafeArea>
  );
}

/** How the golfer's recent swings did on the checks this drill trains, with a link to the latest. */
function YourSwings({ drill, progress }: { drill: Drill; progress: Progress | null }) {
  if (!progress?.swings.length || !drill.targets?.length) return null;
  const latest = [...progress.swings].reverse();
  return (
    <Card style={{ marginTop: 20 }}>
      <Caption>Your swings</Caption>
      {drill.targets.map((t) => {
        const measured = latest.filter((s) => s.checks.some((c) => c.id === t.id)).slice(0, 10);
        if (!measured.length) {
          return <Text key={t.id} style={[type.callout, { color: colors.ink2, marginTop: 8 }]}>{t.name} hasn&apos;t been measured on your swings yet.</Text>;
        }
        const last = measured[0].checks.find((c) => c.id === t.id)!;
        const inRange = measured.filter((s) => s.checks.find((c) => c.id === t.id)?.status === 'good').length;
        return (
          <Pressable key={t.id} accessibilityRole="button" onPress={() => router.push({ pathname: '/swing/[id]', params: { id: measured[0].id } })}
            style={{ marginTop: 10 }}>
            <View style={{ flexDirection: 'row', alignItems: 'center', justifyContent: 'space-between' }}>
              <Text style={[type.headline, { color: colors.ink }]}>{t.name}</Text>
              <StatusText label={last.status === 'good' ? 'In range last time' : 'Out of range last time'} status={last.status} />
            </View>
            <Text style={[type.footnote, { color: colors.ink2, marginTop: 4 }]}>In range on {inRange} of your last {measured.length} swings · tap to see your latest</Text>
          </Pressable>
        );
      })}
    </Card>
  );
}

/** An original demonstration Swing Guru owns (a direct MP4), played natively. */
function OwnedVideo({ uri }: { uri: string }) {
  const player = useVideoPlayer({ uri }, (p) => { p.loop = true; });
  return (
    <View style={styles.video}>
      <VideoView player={player} style={StyleSheet.absoluteFill} contentFit="contain" nativeControls />
    </View>
  );
}

const styles = themed(() => StyleSheet.create({
  screen: { flex: 1, backgroundColor: colors.bg },
  top: { flexDirection: 'row', paddingHorizontal: space.gutter, paddingVertical: 10 },
  round: { width: 44, height: 44, borderRadius: 22, backgroundColor: colors.surface2, alignItems: 'center', justifyContent: 'center' },
  scroll: { paddingHorizontal: space.gutter, paddingBottom: 48 },
  chip: { paddingHorizontal: 12, paddingVertical: 6, borderRadius: radius.pill, backgroundColor: colors.greenSoft },
  video: { aspectRatio: 9 / 16, maxHeight: 520, borderRadius: radius.lg, overflow: 'hidden', backgroundColor: '#000', marginTop: 18 },
  step: { flexDirection: 'row', gap: 14, alignItems: 'flex-start', paddingVertical: 10 },
  stepNo: { width: 30, height: 30, borderRadius: 15, backgroundColor: colors.green, alignItems: 'center', justifyContent: 'center' },
  reps: { flexDirection: 'row', gap: 10, alignItems: 'center', marginTop: 10, padding: 14, borderRadius: radius.md, backgroundColor: colors.greenTint, borderWidth: 1, borderColor: colors.greenLine },
}));
