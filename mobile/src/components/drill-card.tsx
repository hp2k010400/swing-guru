import { router } from 'expo-router';
import { SymbolView } from 'expo-symbols';
import type { ReactNode } from 'react';
import { Pressable, StyleSheet, Text, View } from 'react-native';

import type { Drill } from '@/lib/types';
import { colors, type, themed } from '@/theme';

import { DrillThumb } from './drill-video';
import { Card } from './ui';

/** A drill that expands to show why, steps and reps. Drills are previews, not coach-reviewed. */
export function DrillCard({ drill, open, onToggle, chip }: { drill: Drill; open: boolean; onToggle: () => void; chip?: ReactNode }) {
  return (
    <Card style={{ marginBottom: 10 }} onPress={onToggle}>
      <View style={{ flexDirection: 'row', gap: 14, alignItems: 'center' }}>
        {drill.demo_video ? <DrillThumb /> : (
          <View style={styles.thumb}>
            <SymbolView name="target" size={24} tintColor={colors.green} />
          </View>
        )}
        <View style={{ flex: 1 }}>
          <Text style={[type.headline, { color: colors.ink }]}>{drill.name}</Text>
          <Text style={[type.footnote, { color: colors.ink2, marginTop: 2 }]}>
            {drill.phase_label} · {drill.checkpoint} · {drill.minutes} min
          </Text>
          {chip ? <View style={{ marginTop: 7 }}>{chip}</View> : null}
        </View>
        <SymbolView name={open ? 'chevron.up' : 'chevron.down'} size={14} tintColor={colors.ink3} />
      </View>
      {open ? (
        <View style={{ marginTop: 14 }}>
          <Text style={[type.callout, { color: colors.ink }]}>{drill.why}</Text>
          {drill.steps.map((step, i) => (
            <View key={step} style={{ flexDirection: 'row', gap: 12, marginTop: 10 }}>
              <View style={styles.stepNo}>
                <Text style={{ color: colors.greenInk, fontWeight: '700', fontSize: 13 }}>{i + 1}</Text>
              </View>
              <Text style={[type.callout, { color: colors.ink, flex: 1 }]}>{step}</Text>
            </View>
          ))}
          <Text style={[type.subhead, { color: colors.ink2, marginTop: 12 }]}>{drill.reps}</Text>
          <Pressable accessibilityRole="button" onPress={() => router.push({ pathname: '/drill/[id]', params: { id: drill.id, from: 'finding' } })}
            style={{ flexDirection: 'row', alignItems: 'center', gap: 6, marginTop: 12 }}>
            <Text style={[type.subhead, { color: colors.greenInk, fontWeight: '600' }]}>{drill.demo_video ? 'Watch the video and open drill' : 'Open drill'}</Text>
            <SymbolView name="chevron.right" size={11} tintColor={colors.greenInk} />
          </Pressable>
          <Text style={[type.footnote, { color: colors.ink3, marginTop: 8 }]}>
            Not yet reviewed by a golf professional
          </Text>
        </View>
      ) : null}
    </Card>
  );
}

const styles = themed(() => StyleSheet.create({
  thumb: { width: 56, height: 56, borderRadius: 16, backgroundColor: colors.greenSoft, alignItems: 'center', justifyContent: 'center' },
  stepNo: { width: 24, height: 24, borderRadius: 12, backgroundColor: colors.greenSoft, alignItems: 'center', justifyContent: 'center' },
}));
