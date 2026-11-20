import { Image } from 'expo-image';
import { router } from 'expo-router';
import { SymbolView } from 'expo-symbols';
import { Pressable, StyleSheet, Text, View } from 'react-native';

import type { SwingListItem } from '@/lib/types';
import { colors, radius, themed } from '@/theme';

export function swingDate(s: Pick<SwingListItem, 'captured_at' | 'created_at'>): string {
  const d = new Date(s.captured_at ?? s.created_at);
  return `${d.toLocaleDateString('en-GB', { day: 'numeric', month: 'short' })} · ${d.toLocaleTimeString('en-GB', { hour: '2-digit', minute: '2-digit' })}`;
}

/** Library/Home card: tap opens the Summary; the play button goes straight to the replay. */
export function SwingCard({ swing, width }: { swing: SwingListItem; width: number }) {
  const c = swing.counts;
  const worst = !c ? colors.info : c.problem ? colors.problem : c.attention ? colors.attention : colors.good;
  return (
    <Pressable accessibilityRole="button" accessibilityLabel={`${swing.club ?? 'Swing'}, ${swingDate(swing)}`}
      onPress={() => router.push({ pathname: '/swing/[id]', params: { id: swing.id } })}
      style={({ pressed }) => [styles.card, { width }, pressed && { transform: [{ scale: 0.98 }] }]}>
      {swing.thumb_url ? <Image source={{ uri: swing.thumb_url }} style={StyleSheet.absoluteFill} contentFit="cover" transition={150} /> : null}
      <View style={styles.shade} />
      <View style={styles.bottom}>
        <Text style={styles.club}>{swing.club ?? 'Swing'}</Text>
        <Text style={styles.date}>{swingDate(swing)}</Text>
      </View>
      {swing.score?.available && swing.score.total !== null ? (
        <View style={styles.issue} accessibilityLabel={`Swing Score ${swing.score.total.toFixed(1)}`}>
          <Text style={styles.scoreText}>{swing.score.total.toFixed(1)}</Text>
        </View>
      ) : c ? (
        <View style={styles.issue}>
          <View style={[styles.dot, { backgroundColor: worst }]} />
          <Text style={styles.issueText}>{c.good}/{c.total} in range</Text>
        </View>
      ) : null}
      <Pressable accessibilityLabel="Replay the analysis" onPress={() => router.push({ pathname: '/replay/[id]', params: { id: swing.id } })} style={styles.play}>
        <SymbolView name="play.fill" size={15} tintColor={colors.ink} />
      </Pressable>
    </Pressable>
  );
}

const styles = themed(() => StyleSheet.create({
  card: { aspectRatio: 3 / 4.1, borderRadius: radius.lg, overflow: 'hidden', backgroundColor: '#1b221e' },
  shade: { position: 'absolute', top: 0, right: 0, bottom: 0, left: 0, backgroundColor: 'rgba(0,0,0,0.18)' },
  bottom: { position: 'absolute', left: 12, right: 58, bottom: 12 },
  club: { color: '#fff', fontSize: 17, fontWeight: '700' },
  date: { color: 'rgba(255,255,255,0.85)', fontSize: 13, marginTop: 3 },
  issue: { position: 'absolute', top: 10, right: 10, flexDirection: 'row', alignItems: 'center', gap: 6, paddingHorizontal: 10, paddingVertical: 6, borderRadius: 999, backgroundColor: 'rgba(0,0,0,0.55)' },
  scoreText: { color: '#fff', fontSize: 16, fontWeight: '700', fontVariant: ['tabular-nums'] },
  issueText: { color: '#fff', fontSize: 12, fontWeight: '600' },
  dot: { width: 7, height: 7, borderRadius: 4 },
  play: { position: 'absolute', right: 12, bottom: 12, width: 38, height: 38, borderRadius: 19, backgroundColor: 'rgba(255,255,255,0.94)', alignItems: 'center', justifyContent: 'center' },
}));
