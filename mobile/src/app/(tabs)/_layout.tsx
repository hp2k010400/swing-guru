import { router, Tabs } from 'expo-router';
import { SymbolView, type SymbolViewProps } from 'expo-symbols';
import { Pressable, StyleSheet, View, type ColorValue } from 'react-native';

import { colors, themed } from '@/theme';

const icon = (name: SymbolViewProps['name']) =>
  function TabIcon({ color }: { color: ColorValue }) {
    return <SymbolView name={name} size={24} tintColor={color} />;
  };

export default function TabsLayout() {
  return (
    <Tabs
      screenOptions={{
        headerShown: false,
        tabBarActiveTintColor: colors.green,
        tabBarInactiveTintColor: colors.ink3,
        tabBarStyle: { backgroundColor: colors.bg, borderTopColor: colors.line },
        tabBarLabelStyle: { fontSize: 11, fontWeight: '600' },
      }}>
      <Tabs.Screen name="index" options={{ title: 'Home', tabBarIcon: icon('house.fill') }} />
      <Tabs.Screen name="library" options={{ title: 'Library', tabBarIcon: icon('play.rectangle.on.rectangle') }} />
      <Tabs.Screen
        name="new"
        options={{
          title: 'Analyze',
          tabBarLabel: () => null,
          tabBarAccessibilityLabel: 'Analyze a swing',
          tabBarButton: () => (
            <View style={styles.fabSlot}>
              <Pressable accessibilityRole="button" accessibilityLabel="Analyze a swing" onPress={() => router.push('/analyze')} style={styles.fab}>
                <SymbolView name="plus" size={26} tintColor={colors.onAccent} weight="semibold" />
              </Pressable>
            </View>
          ),
        }}
      />
      <Tabs.Screen name="drills" options={{ title: 'Drills', tabBarIcon: icon('target') }} />
      <Tabs.Screen name="profile" options={{ title: 'Profile', tabBarIcon: icon('person.crop.circle') }} />
    </Tabs>
  );
}

const styles = themed(() => StyleSheet.create({
  fabSlot: { flex: 1, alignItems: 'center' },
  fab: {
    marginTop: -14,
    width: 58,
    height: 58,
    borderRadius: 29,
    backgroundColor: colors.green,
    alignItems: 'center',
    justifyContent: 'center',
    shadowColor: colors.green,
    shadowOpacity: 0.35,
    shadowRadius: 12,
    shadowOffset: { width: 0, height: 8 },
    elevation: 6,
  },
}));
