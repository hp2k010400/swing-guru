// Swing Score UI: the animated ring, the reveal card at the top of the Summary, the shareable card
// layout (rendered to an image later) and the "How is this calculated?" sheet. The score itself is
// computed and stored by the server (swingguru/scoring); nothing here recalculates it.
import { SymbolView } from 'expo-symbols';
import { useEffect, useState, type ReactNode } from 'react';
import { Modal, Pressable, ScrollView, StyleSheet, Text, View } from 'react-native';
import { useReducedMotion } from 'react-native-reanimated';
import Svg, { Circle, Defs, LinearGradient, Stop } from 'react-native-svg';

import { haptic } from '@/lib/haptics';
import { revealFrom } from '@/lib/processing-state';

import type { Counts, SwingScore } from '@/lib/types';
import { colors, radius, shadow, statusColors, type, themed } from '@/theme';

import { Button, Caption } from './ui';

const REVEAL_MS = 1000;
const easeOut = (t: number) => 1 - Math.pow(1 - t, 3);

/** 0 -> 1 over REVEAL_MS (ease-out), or 1 straight away when not animating or under Reduce Motion
 *  (the stored final score is then shown immediately). */
function useReveal(animate: boolean) {
  const reduced = useReducedMotion();
  const run = revealFrom(1, { fresh: animate, reducedMotion: reduced }) === 0;
  const [progress, setProgress] = useState(run ? 0 : 1);
  useEffect(() => {
    if (!run) return;
    let frame = 0;
    const start = Date.now();
    const tick = () => {
      const t = Math.min(1, (Date.now() - start) / REVEAL_MS);
      setProgress(easeOut(t));
      if (t < 1) frame = requestAnimationFrame(tick);
    };
    frame = requestAnimationFrame(tick);
    return () => cancelAnimationFrame(frame);
  }, [run]);
  return progress;
}

export function ScoreRing({ score, size = 176, stroke = 12, animate = false, light = false }: {
  score: number;
  size?: number;
  stroke?: number;
  animate?: boolean;
  light?: boolean; // on a dark background
}) {
  const progress = useReveal(animate);
  const r = (size - stroke) / 2;
  const circumference = 2 * Math.PI * r;
  const fraction = Math.max(0, Math.min(1, score / 10)) * progress;
  return (
    <View style={{ width: size, height: size }} accessibilityRole="image" accessibilityLabel={`Swing Score ${score.toFixed(1)} out of 10`}>
      <Svg width={size} height={size}>
        <Defs>
          <LinearGradient id="sgRing" x1="0" y1="0" x2="1" y2="1">
            <Stop offset="0" stopColor={colors.greenInk} />
            <Stop offset="1" stopColor={colors.green} />
          </LinearGradient>
        </Defs>
        <Circle cx={size / 2} cy={size / 2} r={r} stroke={light ? 'rgba(255,255,255,0.16)' : colors.surface3} strokeWidth={stroke} fill="none" />
        <Circle cx={size / 2} cy={size / 2} r={r} stroke={light ? '#fff' : 'url(#sgRing)'} strokeWidth={stroke} fill="none"
          strokeLinecap="round" strokeDasharray={`${circumference} ${circumference}`} strokeDashoffset={circumference * (1 - fraction)}
          transform={`rotate(-90 ${size / 2} ${size / 2})`} />
      </Svg>
      <View style={styles.ringCentre} pointerEvents="none">
        <Text style={[styles.ringValue, { fontSize: size * 0.3, color: light ? '#fff' : colors.ink }]}>{(score * progress).toFixed(1)}</Text>
        <Text style={[styles.ringScale, { color: light ? 'rgba(255,255,255,0.7)' : colors.ink3 }]}>/ 10</Text>
      </View>
    </View>
  );
}

/** A factual line from the score: how many positions were in range, and where most points went. */
export function scoreSentence(score: SwingScore): string {
  if (!score.checks.length) return '';
  const inRange = score.checks.filter((c) => c.status === 'good').length;
  const lowest = [...score.checks].sort((a, b) => a.points - b.points)[0];
  const head = `${inRange} of ${score.checks.length} positions in range.`;
  if (inRange === score.checks.length) return `${head} Every position we measured was in its target range.`;
  return `${head} Biggest drop: ${phaseLabel(lowest.phase)} ${lowest.name === 'Head' ? 'head movement' : lowest.name.toLowerCase()}.`;
}

/** The lowest-scoring check as a short label ("Impact extension"), or null when every check is in range. */
export function biggestFix(score: SwingScore): string | null {
  if (!score.checks.length || score.checks.every((c) => c.status === 'good')) return null;
  const lowest = [...score.checks].sort((a, b) => a.points - b.points)[0];
  const text = `${phaseLabel(lowest.phase)} ${lowest.name === 'Head' ? 'head movement' : lowest.name.toLowerCase()}`;
  return text.charAt(0).toUpperCase() + text.slice(1);
}

/** Top of the Summary: the payoff after an analysis. */
export function ScoreReveal({ score, sentence, animate, revealKey, onAnalysis, onReplay, onHow }: {
  score: SwingScore;
  sentence: string;
  animate: boolean;
  revealKey: string; // the swing id: one reveal cue per swing at most
  onAnalysis: () => void;
  onReplay: () => void;
  onHow: () => void;
}) {
  // One restrained cue as the ring lands - skipped by the gate when the Ready cue just fired, and
  // never for a historical score (animate is only true for a fresh analysis).
  const scored = score.available;
  useEffect(() => {
    if (!animate || !scored) return;
    const timer = setTimeout(() => haptic('reveal', revealKey), REVEAL_MS);
    return () => clearTimeout(timer);
  }, [animate, scored, revealKey]);
  return (
    <View style={styles.reveal}>
      <Caption style={{ textAlign: 'center', letterSpacing: 1.6 }}>Swing Score</Caption>
      {score.available && score.total !== null ? (
        <>
          <View style={{ alignItems: 'center', marginTop: 16 }}>
            <ScoreRing score={score.total} animate={animate} />
          </View>
          <Text style={[type.subhead, { color: colors.ink2, textAlign: 'center', marginTop: 14 }]}>
            {score.measured} of {score.eligible} score checks measured
          </Text>
        </>
      ) : (
        <View style={styles.unavailable}>
          <SymbolView name="gauge.with.dots.needle.33percent" size={30} tintColor={colors.ink3} />
          <Text style={[type.headline, { color: colors.ink, textAlign: 'center', marginTop: 10 }]}>
            {score.reason ?? 'Not enough reliable measurements for a Swing Score.'}
          </Text>
          <Text style={[type.footnote, { color: colors.ink2, textAlign: 'center', marginTop: 6 }]}>
            {score.measured} of {score.eligible} score checks measured · at least {score.minimum} are needed
          </Text>
        </View>
      )}
      {sentence ? <Text style={[type.callout, { color: colors.ink, textAlign: 'center', marginTop: 12 }]}>{sentence}</Text> : null}
      <View style={{ gap: 6, marginTop: 18 }}>
        <Button label="View analysis" block onPress={onAnalysis} />
        <Button label="Replay swing" icon="play.fill" kind="secondary" block onPress={onReplay} />
      </View>
      <Pressable accessibilityRole="button" onPress={onHow} style={styles.how}>
        <SymbolView name="info.circle" size={14} tintColor={colors.greenInk} />
        <Text style={[type.footnote, { color: colors.greenInk, fontWeight: '600' }]}>How is this calculated?</Text>
      </Pressable>
    </View>
  );
}

/**
 * The score as a self-contained card: everything it shows comes in through props, so the same
 * component can later be rendered off-screen to an image for sharing (Swing Guru · 7.4 / 10 · Driver).
 */
export function ScoreShareCard({ total, club, counts }: { total: number; club: string | null; counts: Counts | null }) {
  return (
    <View style={styles.share}>
      <Text style={styles.shareBrand}>Swing Guru</Text>
      <View style={{ alignItems: 'center', marginVertical: 14 }}>
        <ScoreRing score={total} size={150} light />
      </View>
      <Text style={styles.shareClub}>{club ?? 'Swing'}</Text>
      {counts ? <Text style={styles.shareCounts}>{counts.good} of {counts.total} checks in range</Text> : null}
    </View>
  );
}

export function ScoreHowSheet({ score, visible, onClose }: { score: SwingScore; visible: boolean; onClose: () => void }) {
  return (
    <Modal visible={visible} animationType="slide" presentationStyle="pageSheet" onRequestClose={onClose}>
      <View style={styles.sheet}>
        <View style={styles.sheetHead}>
          <Text style={[type.title2, { color: colors.ink }]}>How is this calculated?</Text>
          <Pressable accessibilityRole="button" accessibilityLabel="Close" onPress={onClose} hitSlop={8} style={styles.close}>
            <SymbolView name="xmark" size={14} tintColor={colors.ink} />
          </Pressable>
        </View>
        <ScrollView contentContainerStyle={{ paddingBottom: 40 }} showsVerticalScrollIndicator={false}>
          <Text style={[type.body, { color: colors.ink }]}>{score.method}</Text>
          <Text style={[type.callout, { color: colors.ink2, marginTop: 10 }]}>
            {score.version === 'swing_score_v1'
              ? "Based on the positions Swing Guru could measure in this swing. The target ranges are Swing Guru's current coaching ranges, not a universal ideal, and every measured position counts equally. Positions we could only estimate count half."
              : "The target ranges are Swing Guru's current coaching ranges, not a universal ideal."}
          </Text>
          <Group title={`Measured (${score.checks.length})`}>
            {score.checks.map((c) => (
              <Row key={c.id} name={`${c.name} · ${phaseLabel(c.phase)}`} detail={`${c.label ?? ''}${c.estimated ? ' · estimated' : ''}`}
                right={<Text style={[type.subhead, { color: statusColors[c.status]?.ink ?? colors.ink }]}>{c.points.toFixed(1)}</Text>} />
            ))}
          </Group>
          {score.excluded.length ? (
            <Group title={`Not measured (${score.excluded.length})`}>
              {score.excluded.map((e) => (
                <Row key={e.id} name={`${e.name} · ${phaseLabel(e.phase)}`} detail={e.reason}
                  right={<Text style={[type.footnote, { color: colors.ink3 }]}>{e.points != null ? 'Neutral' : 'Excluded'}</Text>} />
              ))}
            </Group>
          ) : null}
          <Text style={[type.footnote, { color: colors.ink3, marginTop: 24 }]}>
            {score.method.includes('not your handicap') ? '' : 'Swing Score evaluates this individual swing, not your handicap or overall playing ability. '}Model {score.version}.
          </Text>
        </ScrollView>
      </View>
    </Modal>
  );
}

const PHASES: Record<string, string> = { address: 'Address', mid_back: 'Mid-Back', top: 'Top', downswing: 'Mid-Down', impact: 'Impact' };
const phaseLabel = (phase: string) => PHASES[phase] ?? phase;

function Group({ title, children }: { title: string; children: ReactNode }) {
  return (
    <View style={{ marginTop: 22 }}>
      <Caption style={{ marginBottom: 8 }}>{title}</Caption>
      <View style={styles.group}>{children}</View>
    </View>
  );
}

function Row({ name, detail, right }: { name: string; detail: string; right: ReactNode }) {
  return (
    <View style={styles.row}>
      <View style={{ flex: 1 }}>
        <Text style={[type.subhead, { color: colors.ink }]}>{name}</Text>
        {detail ? <Text style={[type.footnote, { color: colors.ink2, marginTop: 2 }]}>{detail}</Text> : null}
      </View>
      {right}
    </View>
  );
}

const styles = themed(() => StyleSheet.create({
  ringCentre: { position: 'absolute', top: 0, left: 0, right: 0, bottom: 0, alignItems: 'center', justifyContent: 'center' },
  ringValue: { fontWeight: '800', letterSpacing: -1.8, fontVariant: ['tabular-nums'] },
  ringScale: { fontSize: 15, fontWeight: '600', marginTop: -2 },
  reveal: { backgroundColor: colors.surface, borderRadius: radius.xl, padding: 22, borderWidth: 1, borderColor: colors.line, ...shadow },
  unavailable: { alignItems: 'center', paddingVertical: 18, paddingHorizontal: 8 },
  how: { flexDirection: 'row', alignItems: 'center', justifyContent: 'center', gap: 6, marginTop: 14, paddingVertical: 6 },
  share: { width: 300, padding: 24, borderRadius: 28, backgroundColor: colors.greenDeep, alignItems: 'center' },
  shareBrand: { color: 'rgba(255,255,255,0.85)', fontSize: 15, fontWeight: '700', letterSpacing: 1.2, textTransform: 'uppercase' },
  shareClub: { color: '#fff', fontSize: 20, fontWeight: '700' },
  shareCounts: { color: 'rgba(255,255,255,0.8)', fontSize: 15, marginTop: 4 },
  sheet: { flex: 1, backgroundColor: colors.bg, paddingHorizontal: 20, paddingTop: 20 },
  sheetHead: { flexDirection: 'row', alignItems: 'center', justifyContent: 'space-between', marginBottom: 14 },
  close: { width: 44, height: 44, borderRadius: 22, backgroundColor: colors.surface2, alignItems: 'center', justifyContent: 'center' },
  group: { backgroundColor: colors.surface, borderRadius: radius.lg, borderWidth: 1, borderColor: colors.line, overflow: 'hidden' },
  row: { flexDirection: 'row', alignItems: 'center', gap: 12, paddingHorizontal: 16, paddingVertical: 12, borderBottomWidth: StyleSheet.hairlineWidth, borderBottomColor: colors.line },
}));
