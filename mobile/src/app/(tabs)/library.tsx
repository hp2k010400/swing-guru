// Library: every analysed swing, newest first, filterable by club.
import { router } from 'expo-router';
import { useMemo, useState } from 'react';
import { FlatList, Pressable, RefreshControl, StyleSheet, Text, useWindowDimensions, View } from 'react-native';

import { SwingCard } from '@/components/swing-card';
import { Button, EmptyState, LargeTitle, SafeArea } from '@/components/ui';
import { useSwings } from '@/lib/use-swings';
import { colors, radius, space, type, themed } from '@/theme';

export default function Library() {
  const { swings, error, refreshing, reload } = useSwings();
  const [club, setClub] = useState<string | null>(null);
  const { width } = useWindowDimensions();
  const cardWidth = (width - space.gutter * 2 - 12) / 2;
  const clubs = useMemo(() => [...new Set((swings ?? []).map((s) => s.club).filter(Boolean))] as string[], [swings]);
  const shown = (swings ?? []).filter((s) => !club || s.club === club);

  return (
    <SafeArea style={styles.screen} edges={['top']}>
      <FlatList
        data={shown}
        keyExtractor={(s) => s.id}
        numColumns={2}
        columnWrapperStyle={{ gap: 12 }}
        contentContainerStyle={{ paddingHorizontal: space.gutter, paddingBottom: 40, gap: 12 }}
        refreshControl={<RefreshControl refreshing={refreshing} onRefresh={reload} tintColor={colors.green} />}
        ListHeaderComponent={
          <View>
            <LargeTitle right={swings && swings.length >= 2
              ? <Button label="Compare" icon="square.split.2x1" kind="secondary" small onPress={() => router.push('/compare')} />
              : <Text style={[type.footnote, { color: colors.ink2, paddingBottom: 6 }]}>{swings ? `${swings.length} swings` : ''}</Text>}>Library</LargeTitle>
            {clubs.length ? (
              <View style={{ flexDirection: 'row', flexWrap: 'wrap', gap: 8, marginBottom: 14 }}>
                {[null, ...clubs].map((c) => (
                  <Pressable key={c ?? 'all'} onPress={() => setClub(c)} accessibilityState={{ selected: club === c }} style={[styles.filter, club === c && styles.filterOn]}>
                    <Text style={{ color: club === c ? '#fff' : colors.ink2, fontWeight: '600' }}>{c ?? 'All'}</Text>
                  </Pressable>
                ))}
              </View>
            ) : null}
            {error ? <Text style={[type.callout, { color: colors.problemInk, marginBottom: 12 }]}>{error}</Text> : null}
          </View>
        }
        ListEmptyComponent={
          swings ? (
            <EmptyState icon="play.rectangle.on.rectangle" title={club ? 'No swings with this club' : 'No swings yet'}
              text="Every swing you analyze is saved here, so you can see what's changing."
              action={<Button label="Analyze a swing" icon="video.fill" onPress={() => router.push('/analyze')} />} />
          ) : null
        }
        renderItem={({ item }) => <SwingCard swing={item} width={cardWidth} />}
      />
    </SafeArea>
  );
}

const styles = themed(() => StyleSheet.create({
  screen: { flex: 1, backgroundColor: colors.bg },
  filter: { height: 34, paddingHorizontal: 14, borderRadius: radius.pill, borderWidth: 1, borderColor: colors.lineStrong, backgroundColor: colors.surface, justifyContent: 'center' },
  filterOn: { backgroundColor: colors.ink, borderColor: colors.ink },
}));
