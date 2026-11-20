// Swing Guru native components - the same design system as the web app.
import { SymbolView, type SymbolViewProps } from 'expo-symbols';
import type { ReactNode } from 'react';
import { ActivityIndicator, Pressable, ScrollView, StyleSheet, Text, View, type StyleProp, type ViewStyle } from 'react-native';
import Animated, { FadeIn, LayoutAnimationConfig, useReducedMotion, ZoomIn } from 'react-native-reanimated';
import { useSafeAreaInsets } from 'react-native-safe-area-context';

import { haptic } from '@/lib/haptics';
import type { Stage } from '@/lib/types';
import { colors, radius, shadow, space, statusColors, type, type Status, themed } from '@/theme';

/**
 * A screen container padded by the device's real safe-area insets (from the root provider). Used
 * instead of SafeAreaView because a SafeAreaView inside a full-screen modal can measure a zero top
 * inset while the modal is still sliding up, which put header controls under the status bar.
 */
export function SafeArea({ edges = ['top'], style, children }: {
  edges?: ('top' | 'bottom')[];
  style?: StyleProp<ViewStyle>;
  children: ReactNode;
}) {
  const insets = useSafeAreaInsets();
  return (
    <View style={[style, {
      paddingTop: edges.includes('top') ? insets.top : 0,
      paddingBottom: edges.includes('bottom') ? insets.bottom : 0,
    }]}>
      {children}
    </View>
  );
}

export function Screen({ children, scroll = true, edges = ['top'] as ('top' | 'bottom')[] }: {
  children: ReactNode;
  scroll?: boolean;
  edges?: ('top' | 'bottom')[];
}) {
  return (
    <SafeArea style={styles.screen} edges={edges}>
      {scroll ? (
        <ScrollView contentContainerStyle={styles.scroll} showsVerticalScrollIndicator={false}>
          {children}
        </ScrollView>
      ) : (
        <View style={[styles.scroll, { flex: 1 }]}>{children}</View>
      )}
    </SafeArea>
  );
}

export function LargeTitle({ children, right }: { children: string; right?: ReactNode }) {
  return (
    <View style={styles.largeTitle}>
      <Text style={[type.large, { color: colors.ink }]}>{children}</Text>
      {right}
    </View>
  );
}

export function Caption({ children, style }: { children: ReactNode; style?: object }) {
  return <Text style={[type.caption, { color: colors.ink3 }, style]}>{children}</Text>;
}

export function Section({ title, action, children }: { title?: string; action?: ReactNode; children: ReactNode }) {
  return (
    <View style={{ marginTop: space.section }}>
      {title ? (
        <View style={styles.sectionHead}>
          <Text style={[type.title2, { color: colors.ink }]}>{title}</Text>
          {action}
        </View>
      ) : null}
      {children}
    </View>
  );
}

export function Card({ children, tone = 'default', style, onPress }: {
  children: ReactNode;
  tone?: 'default' | 'tint' | 'hero' | 'flat';
  style?: ViewStyle;
  onPress?: () => void;
}) {
  const toneStyle = tone === 'tint' ? styles.cardTint : tone === 'hero' ? styles.cardHero : tone === 'flat' ? styles.cardFlat : styles.cardDefault;
  const body = <View style={[styles.card, toneStyle, style]}>{children}</View>;
  return onPress ? (
    <Pressable onPress={onPress} style={({ pressed }) => ({ transform: [{ scale: pressed ? 0.985 : 1 }] })}>
      {body}
    </Pressable>
  ) : (
    body
  );
}

type ButtonKind = 'primary' | 'secondary' | 'quiet' | 'light' | 'danger';

export function Button({ label, onPress, kind = 'primary', icon, block = false, small = false, busy = false, disabled = false }: {
  label: string;
  onPress: () => void;
  kind?: ButtonKind;
  icon?: SymbolViewProps['name'];
  block?: boolean;
  small?: boolean;
  busy?: boolean;
  disabled?: boolean;
}) {
  const palette = {
    primary: { bg: colors.green, fg: colors.onAccent },
    secondary: { bg: colors.greenSoft, fg: colors.greenInk },
    quiet: { bg: 'transparent', fg: colors.greenInk },
    light: { bg: 'rgba(255,255,255,0.94)', fg: colors.greenDeep },
    danger: { bg: 'transparent', fg: colors.problem },
  }[kind];
  return (
    <Pressable
      accessibilityRole="button"
      accessibilityLabel={label}
      disabled={disabled || busy}
      onPress={() => {
        if (kind === 'primary') haptic('selection'); // light confirmation for the main action only
        onPress();
      }}
      style={({ pressed }) => [
        styles.button,
        { backgroundColor: palette.bg, minHeight: small ? 40 : block ? 54 : 48, opacity: disabled ? 0.45 : 1 },
        block && { alignSelf: 'stretch' },
        pressed && { transform: [{ scale: 0.97 }] },
      ]}>
      {busy ? <ActivityIndicator color={palette.fg} /> : null}
      {!busy && icon ? <SymbolView name={icon} size={small ? 16 : 18} tintColor={palette.fg} /> : null}
      <Text style={{ color: palette.fg, fontSize: small ? 15 : 17, fontWeight: '600' }}>{label}</Text>
    </Pressable>
  );
}

export function StatusChip({ label, status }: { label: string; status: Status }) {
  const c = statusColors[status];
  return (
    <View style={[styles.chip, { backgroundColor: c.soft }]}>
      <View style={[styles.dot, { backgroundColor: c.dot }]} />
      <Text style={{ color: c.ink, fontSize: 13, fontWeight: '600' }}>{label}</Text>
    </View>
  );
}

export function StatusText({ label, status }: { label: string; status: Status }) {
  const c = statusColors[status];
  return (
    <View style={{ flexDirection: 'row', alignItems: 'center', gap: 7, flexShrink: 1 }}>
      <View style={[styles.dot, { backgroundColor: c.dot, width: 9, height: 9 }]} />
      <Text style={{ color: c.ink, fontSize: 15, fontWeight: '600', flexShrink: 1 }} numberOfLines={2}>
        {label}
      </Text>
    </View>
  );
}

export function EmptyState({ icon, title, text, action }: { icon: SymbolViewProps['name']; title: string; text: string; action?: ReactNode }) {
  return (
    <View style={{ alignItems: 'center', paddingVertical: 36, paddingHorizontal: 24 }}>
      <View style={styles.glyph}>
        <SymbolView name={icon} size={28} tintColor={colors.green} />
      </View>
      <Text style={[type.headline, { color: colors.ink, fontSize: 18, textAlign: 'center' }]}>{title}</Text>
      <Text style={[type.callout, { color: colors.ink2, textAlign: 'center', marginTop: 6, marginBottom: 18 }]}>{text}</Text>
      {action}
    </View>
  );
}

/** The real stages. Ticks pop in only on a real transition: rows already done on first render
 *  (hydration) appear without animation, and nothing animates under Reduce Motion. */
export function StageList({ stages, uploading }: { stages: Stage[]; uploading: 'active' | 'done' | 'pending' }) {
  const reduced = useReducedMotion();
  const rows: Stage[] = [{ id: 'upload', label: 'Uploading', state: uploading }, ...stages];
  return (
    <LayoutAnimationConfig skipEntering>
      <View style={styles.stages}>
        {rows.map((s, i) => (
          <View key={s.id} style={styles.stage} accessibilityLabel={`${s.label}: ${s.state === 'done' ? 'done' : s.state === 'active' ? 'in progress' : 'waiting'}`}>
            <View style={styles.markColumn}>
              <View style={[styles.mark, s.state === 'done' && styles.markDone, s.state === 'active' && styles.markActive]}>
                {s.state === 'done' ? (
                  <Animated.View key="done" entering={reduced ? undefined : ZoomIn.springify().damping(14)}>
                    <SymbolView name="checkmark" size={13} tintColor={colors.onAccent} weight="bold" />
                  </Animated.View>
                ) : null}
                {s.state === 'active' ? <ActivityIndicator size="small" color={colors.green} /> : null}
              </View>
              {i < rows.length - 1 ? <View style={[styles.rail, s.state === 'done' && styles.railDone]} /> : null}
            </View>
            <Animated.Text key={s.state} entering={reduced ? undefined : FadeIn.duration(220)}
              style={[type.body, styles.stageLabel, { color: s.state === 'pending' ? colors.ink3 : colors.ink, fontWeight: s.state === 'active' ? '600' : '400' }]}>
              {s.label}
            </Animated.Text>
          </View>
        ))}
      </View>
    </LayoutAnimationConfig>
  );
}

export const styles = themed(() => StyleSheet.create({
  screen: { flex: 1, backgroundColor: colors.bg },
  scroll: { paddingHorizontal: space.gutter, paddingBottom: 48 },
  largeTitle: { paddingTop: 20, paddingBottom: 16, flexDirection: 'row', alignItems: 'flex-end', justifyContent: 'space-between' },
  sectionHead: { flexDirection: 'row', justifyContent: 'space-between', alignItems: 'baseline', marginBottom: 12 },
  card: { borderRadius: radius.lg, padding: space.s5 },
  cardDefault: { backgroundColor: colors.surface, borderWidth: 1, borderColor: colors.line, ...shadow },
  cardTint: { backgroundColor: colors.greenTint, borderWidth: 1, borderColor: colors.greenLine },
  cardHero: { backgroundColor: colors.greenDeep, borderWidth: 1, borderColor: colors.greenLine, ...shadow },
  cardFlat: { backgroundColor: colors.surface, borderWidth: 1, borderColor: colors.line },
  button: { flexDirection: 'row', alignItems: 'center', justifyContent: 'center', gap: 8, paddingHorizontal: 22, borderRadius: radius.pill },
  chip: { flexDirection: 'row', alignItems: 'center', gap: 6, height: 28, paddingHorizontal: 11, borderRadius: radius.pill, alignSelf: 'flex-start' },
  dot: { width: 8, height: 8, borderRadius: 4 },
  glyph: { width: 64, height: 64, borderRadius: 32, backgroundColor: colors.greenSoft, alignItems: 'center', justifyContent: 'center', marginBottom: 16 },
  stages: { marginTop: 30, paddingHorizontal: 4 },
  stage: { flexDirection: 'row', alignItems: 'flex-start', gap: 14, minHeight: 50 },
  markColumn: { alignItems: 'center', width: 26 },
  rail: { width: 2, flex: 1, minHeight: 22, marginVertical: 2, borderRadius: 1, backgroundColor: colors.line },
  railDone: { backgroundColor: colors.green },
  stageLabel: { paddingTop: 2 },
  mark: { width: 26, height: 26, borderRadius: 13, borderWidth: 2, borderColor: colors.lineStrong, alignItems: 'center', justifyContent: 'center' },
  markDone: { backgroundColor: colors.green, borderColor: colors.green },
  markActive: { borderColor: colors.green },
}));
