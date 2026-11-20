// Drills: the ones for your latest swing first, then the library by swing phase. Each opens its page.
import { router } from 'expo-router';
import { SymbolView } from 'expo-symbols';
import { Fragment, useEffect, useState } from 'react';
import { Pressable, StyleSheet, Text, View } from 'react-native';

import { Caption, LargeTitle, Screen } from '@/components/ui';
import { api, friendly } from '@/lib/api';
import type { Drill, Progress } from '@/lib/types';
import { colors, radius, statusColors, type, themed, type Status } from '@/theme';

export default function Drills() {
  const [groups, setGroups] = useState<{ id: string; label: string; drills: Drill[] }[] | null>(null);
  const [progress, setProgress] = useState<Progress | null>(null);
  const [error, setError] = useState<string | null>(null);
  useEffect(() => {
    api.drills().then((d) => setGroups(d.groups)).catch((e) => setError(friendly(e)));
    api.progress().then(setProgress).catch(() => undefined);
  }, []);
  const latest = progress?.swings[progress.swings.length - 1];
  const statusOf = (drill: Drill): Status | null => {
    const t = drill.targets?.[0];
    return latest?.checks.find((c) => c.id === t?.id)?.status ?? null;
  };
  const all = groups?.flatMap((g) => g.drills) ?? [];
  const forYou = all.filter((d) => {
    const s = statusOf(d);
    return s === 'attention' || s === 'problem';
  });
  return (
    <Screen>
      <LargeTitle>Drills</LargeTitle>
      {error ? <Text style={[type.callout, { color: colors.problemInk }]}>{error}</Text> : null}
      {forYou.length ? (
        <>
          <Caption style={{ marginBottom: 10, color: colors.greenInk }}>For your latest swing</Caption>
          {forYou.map((d) => <DrillRow key={d.id} drill={d} status={statusOf(d)} from="finding" />)}
        </>
      ) : null}
      {groups?.map((g) => (
        <Fragment key={g.id}>
          <Caption style={{ marginTop: 22, marginBottom: 10 }}>{g.label}</Caption>
          {g.drills.map((d) => <DrillRow key={d.id} drill={d} status={statusOf(d)} />)}
        </Fragment>
      ))}
      <Text style={[type.footnote, { color: colors.ink3, textAlign: 'center', marginTop: 18 }]}>
        Written guidance from Swing Guru. Not yet reviewed by a golf professional.
      </Text>
    </Screen>
  );
}

function DrillRow({ drill, status, from }: { drill: Drill; status: Status | null; from?: string }) {
  const tone = status ? statusColors[status] : null;
  return (
    <Pressable accessibilityRole="button" accessibilityLabel={`${drill.name}, ${drill.phase_label}, ${drill.minutes} minutes`}
      onPress={() => router.push({ pathname: '/drill/[id]', params: from ? { id: drill.id, from } : { id: drill.id } })}
      style={({ pressed }) => [styles.row, pressed && { transform: [{ scale: 0.985 }] }]}>
      <View style={styles.icon}><SymbolView name="target" size={22} tintColor={colors.greenInk} /></View>
      <View style={{ flex: 1 }}>
        <Text style={[type.headline, { color: colors.ink }]}>{drill.name}</Text>
        <Text style={[type.footnote, { color: colors.ink2, marginTop: 2 }]} numberOfLines={2}>{drill.why}</Text>
        <View style={{ flexDirection: 'row', alignItems: 'center', gap: 8, marginTop: 8 }}>
          <Text style={[type.footnote, { color: colors.ink3 }]}>{drill.phase_label} · {drill.minutes} min</Text>
          {tone && status !== 'good' ? (
            <View style={[styles.flag, { backgroundColor: tone.soft }]}>
              <View style={{ width: 6, height: 6, borderRadius: 3, backgroundColor: tone.dot }} />
              <Text style={{ color: tone.ink, fontSize: 12, fontWeight: '600' }}>Needs work</Text>
            </View>
          ) : null}
        </View>
      </View>
      <SymbolView name="chevron.right" size={13} tintColor={colors.ink3} />
    </Pressable>
  );
}

const styles = themed(() => StyleSheet.create({
  row: { flexDirection: 'row', alignItems: 'center', gap: 14, padding: 16, borderRadius: radius.lg, backgroundColor: colors.surface,
    borderWidth: 1, borderColor: colors.line, marginBottom: 10 },
  icon: { width: 50, height: 50, borderRadius: 15, backgroundColor: colors.greenSoft, alignItems: 'center', justifyContent: 'center' },
  flag: { flexDirection: 'row', alignItems: 'center', gap: 5, paddingHorizontal: 8, paddingVertical: 3, borderRadius: radius.pill },
}));
