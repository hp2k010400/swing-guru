// Swing Guru Rank badges (Bronze III -> Diamond I): layered vector crests with a metal per tier, a
// golf-flag emblem and division chevrons (III = one, I = three). Plus the rank-up celebration.
import { useEffect, useId } from 'react';
import { Modal, StyleSheet, Text, View } from 'react-native';
import Animated, {
  Easing, FadeIn, useAnimatedStyle, useReducedMotion, useSharedValue, withDelay, withRepeat, withSequence, withTiming, ZoomIn,
} from 'react-native-reanimated';
import Svg, { Defs, Ellipse, G, LinearGradient, Path, RadialGradient, Rect, Stop } from 'react-native-svg';

import { haptic } from '@/lib/haptics';
import type { RankInfo } from '@/lib/types';
import { colors, radius, type, themed } from '@/theme';

import { Button, Caption } from './ui';

export type Tier = RankInfo['rank']['tier'];
export type Division = RankInfo['rank']['division'];

// light, mid, dark, rim, emblem
const METALS: Record<Tier | 'locked', [string, string, string, string, string]> = {
  Bronze: ['#f6cfa0', '#c27a43', '#6d3a1a', '#3d1f0c', '#fff1de'],
  Silver: ['#ffffff', '#c3cbd3', '#6c7680', '#353c43', '#ffffff'],
  Gold: ['#fff4c2', '#d8b45e', '#8d6522', '#45300e', '#fffaf0'],
  Platinum: ['#effdff', '#a9dde5', '#3f8794', '#1b434b', '#ffffff'],
  Diamond: ['#f3eeff', '#a9b9ff', '#5b4fd8', '#251e66', '#ffffff'],
  locked: ['#9aa19d', '#6f7a74', '#3a423e', '#232a27', '#c9cfcb'],
};

export const TIER_ACCENT: Record<Tier, string> = {
  Bronze: '#c27a43', Silver: '#aab3bc', Gold: '#d6b56a', Platinum: '#6fc3cf', Diamond: '#8f8cff',
};

const CREST = 'M60 4 L108 22 L108 66 C108 96 86 116 60 128 C34 116 12 96 12 66 L12 22 Z';
const PANEL = 'M60 16 L97 30 L97 66 C97 89 80 105 60 115 C40 105 23 89 23 66 L23 30 Z';
const SHEEN = 'M12 22 L60 4 L108 22 L108 42 C80 34 42 42 12 62 Z';

function sparkle(x: number, y: number, r: number) {
  return `M${x} ${y - r} L${x + r * 0.28} ${y - r * 0.28} L${x + r} ${y} L${x + r * 0.28} ${y + r * 0.28} L${x} ${y + r} ` +
    `L${x - r * 0.28} ${y + r * 0.28} L${x - r} ${y} L${x - r * 0.28} ${y - r * 0.28} Z`;
}

export function RankBadge({ tier, division, size = 96, locked = false }: { tier: Tier; division: Division; size?: number; locked?: boolean }) {
  const uid = useId().replace(/[^a-zA-Z0-9]/g, '');
  const [light, mid, dark, rim, emblem] = METALS[locked ? 'locked' : tier];
  const pips = division === 'III' ? 1 : division === 'II' ? 2 : 3;
  const sparkles = locked ? [] : tier === 'Diamond' ? [[22, 28, 6], [98, 30, 5], [104, 70, 4]] : tier === 'Platinum' ? [[98, 30, 5]] : [];
  return (
    <View style={{ width: size, height: size * 1.1, opacity: locked ? 0.55 : 1 }}
      accessibilityRole="image" accessibilityLabel={`${tier} ${division}${locked ? ', not reached yet' : ''}`}>
      <Svg width={size} height={size * 1.1} viewBox="0 0 120 132">
        <Defs>
          <LinearGradient id={`m${uid}`} x1="0.15" y1="0" x2="0.85" y2="1">
            <Stop offset="0" stopColor={light} />
            <Stop offset="0.45" stopColor={mid} />
            <Stop offset="1" stopColor={dark} />
          </LinearGradient>
          <LinearGradient id={`p${uid}`} x1="0.5" y1="0" x2="0.5" y2="1">
            <Stop offset="0" stopColor={dark} />
            <Stop offset="1" stopColor={rim} />
          </LinearGradient>
          <RadialGradient id={`g${uid}`} cx="0.5" cy="0.42" r="0.55">
            <Stop offset="0" stopColor={mid} stopOpacity={0.55} />
            <Stop offset="1" stopColor={mid} stopOpacity={0} />
          </RadialGradient>
          <LinearGradient id={`s${uid}`} x1="0" y1="0" x2="0.6" y2="1">
            <Stop offset="0" stopColor="#ffffff" stopOpacity={0.5} />
            <Stop offset="1" stopColor="#ffffff" stopOpacity={0} />
          </LinearGradient>
        </Defs>
        <Path d={CREST} fill={`url(#m${uid})`} stroke={rim} strokeWidth={2} strokeLinejoin="round" />
        <Path d={PANEL} fill={`url(#p${uid})`} />
        <Path d={PANEL} fill={`url(#g${uid})`} />
        <Path d={PANEL} fill="none" stroke={light} strokeOpacity={0.55} strokeWidth={1.2} strokeLinejoin="round" />
        <Path d={SHEEN} fill={`url(#s${uid})`} />
        <G>
          <Ellipse cx={60} cy={80} rx={17} ry={4.2} fill={emblem} opacity={0.32} />
          <Rect x={58.4} y={36} width={3.2} height={44} rx={1.6} fill={emblem} />
          <Path d="M61.5 37 L84 45.5 L61.5 54 Z" fill={emblem} />
        </G>
        {Array.from({ length: pips }).map((_, i) => {
          const y = 89 + i * 7;
          return <Path key={i} d={`M52 ${y} L60 ${y + 5} L68 ${y}`} fill="none" stroke={emblem} strokeWidth={2.6}
            strokeLinecap="round" strokeLinejoin="round" opacity={0.95} />;
        })}
        {sparkles.map(([x, y, r], i) => <Path key={i} d={sparkle(x, y, r)} fill="#ffffff" opacity={0.9} />)}
      </Svg>
    </View>
  );
}

/** Full-screen moment when a new rank is reached: the badge springs in over a slow glow. */
export function RankCelebration({ rank, onClose }: { rank: RankInfo['rank'] | null; onClose: () => void }) {
  const reduced = useReducedMotion();
  const glow = useSharedValue(0);
  useEffect(() => {
    if (!rank) return;
    haptic('success', `rank-${rank.index}`);
    if (reduced) return;
    glow.value = withDelay(250, withRepeat(withSequence(
      withTiming(1, { duration: 1400, easing: Easing.inOut(Easing.quad) }),
      withTiming(0.45, { duration: 1400, easing: Easing.inOut(Easing.quad) })), -1, false));
  }, [rank, reduced, glow]);
  const halo = useAnimatedStyle(() => ({ opacity: 0.25 + 0.5 * glow.value, transform: [{ scale: 0.9 + 0.18 * glow.value }] }));
  if (!rank) return null;
  const accent = TIER_ACCENT[rank.tier];
  return (
    <Modal transparent animationType="fade" visible onRequestClose={onClose}>
      <View style={styles.scrim}>
        <Animated.View entering={reduced ? undefined : FadeIn.duration(300)} style={styles.card}>
          <Caption style={{ color: colors.gold, textAlign: 'center', letterSpacing: 2.4 }}>Rank up</Caption>
          <View style={styles.badgeWrap}>
            <Animated.View style={[styles.halo, halo]} pointerEvents="none">
              <Svg width={260} height={260}>
                <Defs>
                  <RadialGradient id="rankGlow" cx="0.5" cy="0.5" r="0.5">
                    <Stop offset="0" stopColor={accent} stopOpacity={0.85} />
                    <Stop offset="0.55" stopColor={accent} stopOpacity={0.25} />
                    <Stop offset="1" stopColor={accent} stopOpacity={0} />
                  </RadialGradient>
                </Defs>
                <Rect x={0} y={0} width={260} height={260} fill="url(#rankGlow)" />
              </Svg>
            </Animated.View>
            <Animated.View entering={reduced ? undefined : ZoomIn.springify().damping(11).delay(120)}>
              <RankBadge tier={rank.tier} division={rank.division} size={150} />
            </Animated.View>
          </View>
          <Text style={[type.title1, { color: colors.ink, textAlign: 'center' }]}>{rank.name}</Text>
          <Text style={[type.callout, { color: colors.ink2, textAlign: 'center', marginTop: 8 }]}>
            Earned through better swings and steady practice. Keep going.
          </Text>
          <View style={{ marginTop: 22 }}>
            <Button label="Continue" block onPress={onClose} />
          </View>
        </Animated.View>
      </View>
    </Modal>
  );
}

const styles = themed(() => StyleSheet.create({
  scrim: { flex: 1, backgroundColor: 'rgba(0,0,0,0.72)', justifyContent: 'center', padding: 24 },
  card: { backgroundColor: colors.surface, borderRadius: radius.xl, padding: 24, borderWidth: 1, borderColor: colors.goldLine },
  badgeWrap: { alignItems: 'center', justifyContent: 'center', height: 210, marginVertical: 8 },
  halo: { position: 'absolute', width: 260, height: 260 },
}));
