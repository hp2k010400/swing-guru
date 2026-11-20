// Home building blocks. Everything shown is measured (counts of checks in range), never a score.
import { router } from 'expo-router';
import { SymbolView, type SymbolViewProps } from 'expo-symbols';
import { Pressable, StyleSheet, Text, View } from 'react-native';

import type { Milestone, Recurring } from '@/lib/home-stats';
import type { SwingListItem } from '@/lib/types';
import { colors, radius, shadow, statusColors, type, themed } from '@/theme';

import { Card, Caption } from './ui';

export function StatTile({ icon, value, label, onPress }: { icon: SymbolViewProps['name']; value: string; label: string; onPress?: () => void }) {
  return (
    <Pressable accessibilityRole="button" accessibilityLabel={`${label}: ${value}`} onPress={onPress}
      style={({ pressed }) => [styles.tile, pressed && { opacity: 0.85 }]}>
      <View style={styles.tileIcon}>
        <SymbolView name={icon} size={18} tintColor={colors.green} />
      </View>
      <Text style={styles.tileValue} numberOfLines={1} adjustsFontSizeToFit>{value}</Text>
      <View style={{ flexDirection: 'row', alignItems: 'center', gap: 3 }}>
        <Text style={[type.footnote, { color: colors.ink2 }]} numberOfLines={1}>{label}</Text>
        {onPress ? <SymbolView name="chevron.right" size={10} tintColor={colors.ink3} /> : null}
      </View>
    </Pressable>
  );
}

/** "Most often out of range": a fact about recent swings, with one dot per swing (oldest -> newest). */
export function RecurringCard({ item }: { item: Recurring }) {
  return (
    <Pressable accessibilityRole="button" onPress={() => router.push({ pathname: '/swing/[id]', params: { id: item.latestSwingId } })}
      style={({ pressed }) => [styles.recurring, pressed && { opacity: 0.9 }]}>
      <View style={{ flexDirection: 'row', alignItems: 'center', gap: 8 }}>
        <SymbolView name="scope" size={16} tintColor={colors.green} />
        <Caption style={{ color: colors.greenInk, flex: 1 }}>Most often out of range</Caption>
        <SymbolView name="chevron.right" size={13} tintColor={colors.ink3} />
      </View>
      <Text style={[type.title2, { color: colors.ink, marginTop: 10 }]}>{item.name} <Text style={{ color: colors.ink2, fontWeight: '500' }}>at {item.phaseLabel}</Text></Text>
      <View style={{ flexDirection: 'row', gap: 6, marginTop: 14 }}>
        {item.history.map((status, i) => (
          <View key={i} style={[styles.pip, { backgroundColor: status ? statusColors[status].dot : colors.surface3 }]} />
        ))}
      </View>
      <Text style={[type.callout, { color: colors.ink2, marginTop: 12 }]}>
        Out of range in {item.out} of your last {item.outOf} {item.outOf === 1 ? 'swing' : 'swings'}. Tap to see it and a drill.
      </Text>
    </Pressable>
  );
}

/** One stacked bar per swing: checks in range / slightly out / clearly out. Oldest on the left. */
export function ChecksChart({ swings }: { swings: SwingListItem[] }) {
  const rows = swings.filter((s) => s.counts && s.counts.total > 0).slice(0, 10).reverse();
  if (!rows.length) return null;
  return (
    <Card>
      <View style={{ flexDirection: 'row', justifyContent: 'space-between', alignItems: 'baseline' }}>
        <Text style={[type.headline, { color: colors.ink, fontSize: 19 }]}>Checks in range</Text>
        <Text style={[type.footnote, { color: colors.ink3 }]}>Last {rows.length} {rows.length === 1 ? 'swing' : 'swings'}</Text>
      </View>
      <View style={styles.chart}>
        {rows.map((s) => {
          const c = s.counts!;
          return (
            <Pressable key={s.id} accessibilityLabel={`${c.good} of ${c.total} in range`} style={styles.column}
              onPress={() => router.push({ pathname: '/swing/[id]', params: { id: s.id } })}>
              <Text style={styles.columnLabel}>{c.good}/{c.total}</Text>
              <View style={styles.bar}>
                {c.problem ? <View style={{ flex: c.problem, backgroundColor: colors.problem }} /> : null}
                {c.attention ? <View style={{ flex: c.attention, backgroundColor: colors.attention }} /> : null}
                {c.good ? <View style={{ flex: c.good, backgroundColor: colors.good }} /> : null}
              </View>
            </Pressable>
          );
        })}
      </View>
      <View style={{ flexDirection: 'row', gap: 14, marginTop: 12 }}>
        {(['good', 'attention', 'problem'] as const).map((k) => (
          <View key={k} style={{ flexDirection: 'row', alignItems: 'center', gap: 6 }}>
            <View style={{ width: 8, height: 8, borderRadius: 4, backgroundColor: colors[k] }} />
            <Text style={[type.footnote, { color: colors.ink2 }]}>{k === 'good' ? 'In range' : k === 'attention' ? 'Slightly out' : 'Clearly out'}</Text>
          </View>
        ))}
      </View>
    </Card>
  );
}

export function Milestones({ items }: { items: Milestone[] }) {
  return (
    <View style={styles.badges}>
      {items.map((m) => (
        <View key={m.id} style={styles.badgeWrap} accessibilityLabel={`${m.title}${m.earned ? ', earned' : ', not yet'}`}>
          <View style={[styles.badge, m.earned ? styles.badgeOn : styles.badgeOff]}>
            <SymbolView name={m.icon} size={24} tintColor={m.earned ? colors.gold : colors.ink3} />
          </View>
          <Text style={[type.footnote, { color: m.earned ? colors.ink : colors.ink3, textAlign: 'center', marginTop: 6 }]} numberOfLines={2}>{m.title}</Text>
        </View>
      ))}
    </View>
  );
}

const styles = themed(() => StyleSheet.create({
  tile: { flex: 1, padding: 14, borderRadius: radius.lg, backgroundColor: colors.surface, borderWidth: 1, borderColor: colors.line, gap: 6 },
  tileIcon: { width: 34, height: 34, borderRadius: 10, backgroundColor: colors.greenSoft, alignItems: 'center', justifyContent: 'center', marginBottom: 6 },
  tileValue: { fontSize: 30, fontWeight: '800', color: colors.ink, letterSpacing: -0.8, fontVariant: ['tabular-nums'] },
  recurring: { padding: 18, borderRadius: radius.lg, backgroundColor: colors.greenTint, borderWidth: 1, borderColor: colors.greenLine },
  pip: { flex: 1, height: 8, borderRadius: 4 },
  chart: { flexDirection: 'row', alignItems: 'flex-end', justifyContent: 'space-around', gap: 10, height: 150, marginTop: 18 },
  column: { flex: 1, height: '100%', justifyContent: 'flex-end', alignItems: 'center', maxWidth: 52 },
  columnLabel: { fontSize: 11, fontWeight: '600', color: colors.ink2, marginBottom: 4 },
  bar: { width: '100%', flex: 1, borderRadius: 6, overflow: 'hidden', flexDirection: 'column' },
  badges: { flexDirection: 'row', flexWrap: 'wrap', rowGap: 14 },
  badgeWrap: { width: '25%', alignItems: 'center', paddingHorizontal: 4 },
  badge: { width: 60, height: 60, borderRadius: 30, alignItems: 'center', justifyContent: 'center', borderWidth: 2 },
  badgeOn: { backgroundColor: colors.goldSoft, borderColor: colors.goldLine, ...shadow },
  badgeOff: { backgroundColor: colors.surface2, borderColor: colors.line },
}));
