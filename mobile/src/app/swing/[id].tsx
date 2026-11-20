// Swing Summary: the Swing Score reveal, how the swing went, what needs attention, what to do next.
// The score is the server's stored, versioned Swing Score; no "Start here" unless an interpretation
// rule is eligible.
import { router, useLocalSearchParams } from 'expo-router';
import { SymbolView } from 'expo-symbols';
import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { ActivityIndicator, Alert, Pressable, ScrollView, StyleSheet, Text, View } from 'react-native';

import { DrillCard } from '@/components/drill-card';
import { AnalysisFeedback } from '@/components/feedback-card';
import { ProOfferCard } from '@/components/pro-offer';
import { FirstSwingReminderCard } from '@/components/first-swing-reminder';
import { ReminderCard } from '@/components/reminder-card';
import { SaveSwingCard } from '@/components/save-swing';
import { maybeAskForRating } from '@/lib/rating';
import { track } from '@/lib/analytics';
import { biggestFix, ScoreHowSheet, ScoreReveal, scoreSentence } from '@/components/score';
import { Button, Caption, Card, EmptyState, SafeArea, Section, StatusChip, StatusText } from '@/components/ui';
import { api, cachedSwing, EXAMPLE_ID, friendly } from '@/lib/api';
import { createCloseGuard } from '@/lib/close-guard';
import { onClubChanged } from '@/lib/club-events';
import { isFirstResult } from '@/lib/first-result';
import { useSwings } from '@/lib/use-swings';
import type { BreakdownRow, SwingDetail } from '@/lib/types';
import { colors, radius, space, statusColors, type, themed } from '@/theme';

const viewerCheckId = (id: string) => id.replace('.', ':'); // "impact.pelvis" -> "impact:pelvis"

/** On the example only: golfers with no swings of their own get "Remind me at my next practice". */
function ExampleFirstSwingReminder() {
  const { swings } = useSwings();
  return <FirstSwingReminderCard from="example" ownSwings={swings ? swings.length : null} />;
}

export default function Summary() {
  const { id, fresh, drill } = useLocalSearchParams<{ id: string; fresh?: string; drill?: string }>();
  // Opens instantly when Processing preloaded this swing (same account only); refreshed below.
  const [swing, setSwing] = useState<SwingDetail | null>(() => cachedSwing(id));
  const [error, setError] = useState<string | null>(null);
  const [openDrill, setOpenDrill] = useState<string | null>(drill ?? null);
  const [how, setHow] = useState(false);
  const [openCheck, setOpenCheck] = useState<string | null>(null);
  const scroller = useRef<ScrollView>(null);
  const [anchors, setAnchors] = useState<{ breakdown?: number; practice?: number }>({});
  const close = useMemo(() => createCloseGuard(() => (router.canGoBack() ? router.back() : router.replace('/'))), []);
  const scrollTo = (y?: number) => {
    if (y !== undefined) scroller.current?.scrollTo({ y: Math.max(0, y - 12), animated: true });
  };

  // The shared example (new users): read-only, nothing to edit, delete, rate or buy from here.
  const example = id === EXAMPLE_ID;
  // The first-result Pro offer: a just-finished result, or the golfer's only completed swing opened from
  // anywhere (leaving Processing early must not skip it). Auto-open is still once per device.
  const { swings: ownSwings } = useSwings();
  const firstResult = isFirstResult({ fresh: Boolean(fresh), example, ownCompleted: ownSwings ? ownSwings.length : null });
  const openedExample = useRef(false);
  useEffect(() => {
    if (!example || !swing || openedExample.current) return;
    openedExample.current = true;
    track('example_opened', {});
  }, [example, swing]);
  const fetchSwing = useCallback(() => {
    (id === EXAMPLE_ID ? api.example() : api.swing(id)).then(setSwing).catch((e) => setError(friendly(e)));
  }, [id]);
  useEffect(fetchSwing, [fetchSwing]);
  // The club is chosen before analysis; changing it later happens on the club screen.
  // Arriving from the replay's "Practise" button: open that drill and bring it into view.
  const practiceY = anchors.practice;
  useEffect(() => {
    if (!drill || practiceY === undefined) return;
    const timer = setTimeout(() => scroller.current?.scrollTo({ y: Math.max(0, practiceY - 12), animated: true }), 250);
    return () => clearTimeout(timer);
  }, [drill, practiceY]);
  // Apple's rating prompt after a good just-finished result (rules in lib/rating-core.ts).
  // Cancelled only when leaving the screen, not when the swing refreshes (it does once on open).
  const rated = useRef<string | null>(null);
  const cancelRating = useRef<() => void>(() => undefined);
  useEffect(() => {
    if (!fresh || !swing || swing.id !== id || rated.current === swing.id) return;
    rated.current = swing.id;
    cancelRating.current = maybeAskForRating(swing.score);
  }, [fresh, swing, id]);
  useEffect(() => () => cancelRating.current(), []);
  useEffect(() => onClubChanged((swingId, club) => {
    if (swingId === id) setSwing((current) => (current ? { ...current, club } : current));
  }), [id]);
  const load = () => {
    setError(null);
    fetchSwing();
  };

  if (error) {
    return (
      <SafeArea style={styles.screen} edges={['top', 'bottom']}>
        <EmptyState icon="wifi.exclamationmark" title="Couldn't load this swing" text={error}
          action={<Button label="Try again" onPress={load} />} />
      </SafeArea>
    );
  }
  if (!swing) {
    return (
      <SafeArea style={[styles.screen, { alignItems: 'center', justifyContent: 'center' }]} edges={['top', 'bottom']}>
        <ActivityIndicator color={colors.green} />
      </SafeArea>
    );
  }

  const h = swing.headline;
  const when = new Date(swing.captured_at ?? swing.created_at);
  const remove = () =>
    Alert.alert('Delete this swing?', 'The video and its analysis will be removed from your account.', [
      { text: 'Cancel', style: 'cancel' },
      {
        text: 'Delete', style: 'destructive',
        onPress: async () => {
          try {
            await api.deleteSwing(swing.id);
            router.replace('/library');
          } catch (e) {
            Alert.alert("Couldn't delete the swing", friendly(e));
          }
        },
      },
    ]);
  const routeId = example ? EXAMPLE_ID : swing.id;
  const replay = (check?: string) => router.push({ pathname: '/replay/[id]', params: check ? { id: routeId, check } : { id: routeId } });
  const analyseOwn = () => {
    track('example_cta_tapped', { from: 'summary' });
    router.dismissAll();
    router.push('/analyze');
  };

  return (
    <SafeArea style={styles.screen} edges={example ? ['top', 'bottom'] : ['top']}>
      <ScrollView ref={scroller} contentContainerStyle={styles.scroll} showsVerticalScrollIndicator={false}>
        <View style={styles.top}>
          <Pressable accessibilityRole="button" accessibilityLabel="Close" onPress={close} hitSlop={8}
            style={({ pressed }) => [styles.round, pressed && { opacity: 0.6 }]}>
            <SymbolView name="xmark" size={16} tintColor={colors.ink} />
          </Pressable>
          {example ? (
            <View style={styles.exampleBadge}><Text style={[type.footnote, { color: colors.greenInk, fontWeight: '700' }]}>EXAMPLE</Text></View>
          ) : (
            <Text style={[type.subhead, { color: colors.ink }]}>
              {when.toLocaleDateString('en-GB', { day: 'numeric', month: 'short' })} · {when.toLocaleTimeString('en-GB', { hour: '2-digit', minute: '2-digit' })}
            </Text>
          )}
        </View>
        {example ? <Note icon="info.circle" text="This is an example analysis of a real swing, so you can see what you'll get. It isn't saved to your Library and doesn't use your free analysis." /> : null}

        {fresh ? <Note icon="checkmark.circle.fill" tone="good" text="Saved to your Library" /> : null}
        {swing.notice ? <Note icon="info.circle" text={swing.notice} /> : null}

        {example ? (
          <View style={styles.club}>
            <SymbolView name="figure.golf" size={15} tintColor={colors.green} />
            <Text style={[type.subhead, { color: colors.ink }]}>{swing.club ?? 'Driver'}</Text>
          </View>
        ) : (
        <Pressable accessibilityRole="button" accessibilityLabel={swing.club ? `Club: ${swing.club}. Change` : 'Add the club you used'}
          onPress={() => router.push({ pathname: '/club', params: { swing: swing.id, current: swing.club ?? '' } })}
          style={({ pressed }) => [styles.club, pressed && { backgroundColor: colors.surface2 }]}>
          <SymbolView name="figure.golf" size={15} tintColor={colors.green} />
          <Text style={[type.subhead, { color: swing.club ? colors.ink : colors.ink2 }]}>{swing.club ?? 'Club not set'}</Text>
          <Text style={[type.footnote, { color: colors.green, fontWeight: '600' }]}>{swing.club ? 'Change' : 'Add'}</Text>
        </Pressable>
        )}

        <ScoreReveal score={swing.score} sentence={scoreSentence(swing.score)} animate={Boolean(fresh)} revealKey={swing.id}
          onAnalysis={() => scrollTo(anchors.breakdown)} onReplay={() => replay()} onHow={() => setHow(true)} />
        <ScoreHowSheet score={swing.score} visible={how} onClose={() => setHow(false)} />
        <SaveSwingCard fresh={Boolean(fresh)} example={example} />
        <ReminderCard swingId={swing.id} fresh={Boolean(fresh)} example={example} fix={biggestFix(swing.score)} club={swing.club ?? null} />
        {firstResult ? <ProOfferCard fix={biggestFix(swing.score)} /> : null}

        <Card style={{ marginTop: 16 }}>
          <Caption>Checks</Caption>
          <View style={{ flexDirection: 'row', alignItems: 'baseline', gap: 10, marginTop: 8 }}>
            <Text style={{ fontSize: 56, fontWeight: '700', color: colors.ink, letterSpacing: -1.5 }}>{h.good}</Text>
            <Text style={{ fontSize: 18, fontWeight: '600', color: colors.ink2 }}>of {h.total} checks in range</Text>
          </View>
          <Text style={[type.footnote, { color: colors.ink2 }]}>{h.phases_analysed} phases analysed</Text>
          <View style={styles.bar}>
            {(['good', 'attention', 'problem'] as const).map((k) => (h[k] ? <View key={k} style={{ flex: h[k], backgroundColor: colors[k], borderRadius: 4 }} /> : null))}
          </View>
          <View style={{ flexDirection: 'row', flexWrap: 'wrap', gap: 14, marginTop: 12 }}>
            <StatusText label={`${h.good} in range`} status="good" />
            {h.attention ? <StatusText label={`${h.attention} slightly out`} status="attention" /> : null}
            {h.problem ? <StatusText label={`${h.problem} clearly out`} status="problem" /> : null}
          </View>
        </Card>

        <Text style={[type.body, { color: colors.ink, marginTop: 18, fontSize: 16.5, lineHeight: 24 }]}>{swing.summary_text}</Text>
        {swing.camera.moved && swing.camera.corrected_phases.length ? (
          <Note icon="info.circle" text="Your phone moved during the swing. We corrected for it where we could." />
        ) : null}

        <Card tone="tint" style={{ marginTop: 18 }}>
          <Caption style={{ color: colors.green }}>Start here</Caption>
          {swing.focus.available && swing.focus.source !== 'stub' ? (
            <>
              <Text style={[type.headline, { color: colors.ink, fontSize: 19, marginTop: 8 }]}>{swing.focus.title}</Text>
              {swing.focus.label && swing.focus.status ? <View style={{ marginTop: 8 }}><StatusChip label={swing.focus.label} status={swing.focus.status} /></View> : null}
              <Text style={[type.callout, { color: colors.ink2, marginTop: 6 }]}>{swing.focus.message}</Text>
            </>
          ) : (
            <Text style={[type.callout, { color: colors.ink2, marginTop: 8 }]}>Your focus will appear here as Swing Guru learns your swing.</Text>
          )}
        </Card>

        <View onLayout={(e) => { const y = e.nativeEvent.layout.y; setAnchors((a) => (a.breakdown === y ? a : { ...a, breakdown: y })); }}>
        <Section title="Breakdown">
          {swing.breakdown.map((group) => (
            <View key={group.phase}>
              <Caption style={{ marginTop: 18, marginBottom: 10 }}>{group.label}</Caption>
              {group.rows.map((row) => (
                row.kind === 'check' && row.coaching ? (
                  <CheckCard key={row.id} row={row} open={openCheck === row.id}
                    onToggle={() => setOpenCheck(openCheck === row.id ? null : row.id)}
                    onReplay={() => replay(viewerCheckId(row.id))} />
                ) : (
                  <Row key={row.id} row={row} onPress={row.kind === 'withheld' ? undefined : () => replay(viewerCheckId(row.id))} />
                )
              ))}
            </View>
          ))}
          <Text style={[type.footnote, { color: colors.ink3, marginTop: 12 }]}>
            Takeaway is in the replay but isn&apos;t scored yet. Tap a check for what it means and how to improve it.
          </Text>
        </Section>
        </View>

        <View onLayout={(e) => { const y = e.nativeEvent.layout.y; setAnchors((a) => (a.practice === y ? a : { ...a, practice: y })); }}>
        <Section title="Practice">
          {swing.recommended.length ? (
            swing.recommended.map(({ drill, checkpoint }) => (
              <DrillCard key={drill.id} drill={drill} open={openDrill === drill.id} onToggle={() => setOpenDrill(openDrill === drill.id ? null : drill.id)}
                chip={<StatusChip label={`${checkpoint.name}: ${checkpoint.label}`} status={checkpoint.status} />} />
            ))
          ) : (
            <Card tone="flat"><Text style={[type.callout, { color: colors.ink2 }]}>Every check we measured was in range.</Text></Card>
          )}
          <Text style={[type.footnote, { color: colors.ink3, marginTop: 10 }]}>Preview drills · not yet reviewed by a coach</Text>
        </Section>
        </View>

        {example ? (
          <ExampleFirstSwingReminder />
        ) : (
          <>
            <View style={{ marginTop: 22 }}>
              <Button label="Compare with another swing" icon="square.split.2x1" kind="secondary" block
                onPress={() => router.push({ pathname: '/compare', params: { with: swing.id } })} />
            </View>

            <AnalysisFeedback swingId={swing.id} measuredChecks={swing.score.checks.map((c) => c.id)} />

            <View style={{ marginTop: 26, gap: 4 }}>
              <Button label="Done" kind="quiet" block onPress={() => router.replace('/')} />
              <Button label="Delete swing" kind="danger" small onPress={remove} />
            </View>
          </>
        )}
      </ScrollView>
      {/* The example is a long page: its two ways forward stay pinned to the bottom, always visible. */}
      {example ? (
        <View style={styles.exampleBar}>
          <Button label="Analyse your own swing" icon="sparkles" block onPress={analyseOwn} />
          <Button label="Done" kind="quiet" block onPress={() => router.replace('/')} />
        </View>
      ) : null}
    </SafeArea>
  );
}

function Note({ icon, text, tone = 'info' }: { icon: 'info.circle' | 'checkmark.circle.fill'; text: string; tone?: 'info' | 'good' }) {
  const good = tone === 'good';
  return (
    <View style={[styles.note, { backgroundColor: good ? colors.goodSoft : colors.surface2 }]}>
      <SymbolView name={icon} size={16} tintColor={good ? colors.goodInk : colors.ink3} />
      <Text style={[type.footnote, { color: good ? colors.goodInk : colors.ink2, flex: 1 }]}>{text}</Text>
    </View>
  );
}

/** A measured check as a coaching card: the plain headline, then what we saw, why it matters and
 *  what to try. The verdict label stays as the chip, so nothing about the result changes. */
function CheckCard({ row, open, onToggle, onReplay }: { row: BreakdownRow; open: boolean; onToggle: () => void; onReplay: () => void }) {
  const c = row.coaching!;
  const tone = statusColors[row.status];
  return (
    <View style={[styles.check, open && { borderColor: colors.lineStrong }]}>
      <Pressable accessibilityRole="button" accessibilityState={{ expanded: open }}
        accessibilityLabel={`${row.name}: ${c.headline}. ${row.label ?? ''}`} onPress={onToggle}
        style={({ pressed }) => [styles.checkHead, pressed && { opacity: 0.8 }]}>
        <View style={[styles.checkRail, { backgroundColor: tone.dot }]} />
        <View style={{ flex: 1 }}>
          <Text style={[type.caption, { color: colors.ink3, letterSpacing: 1 }]}>{row.name}</Text>
          <Text style={[type.headline, { color: colors.ink, marginTop: 3 }]}>{c.headline}</Text>
        </View>
        {row.label ? <StatusChip label={row.label} status={row.status} /> : null}
        <SymbolView name={open ? 'chevron.up' : 'chevron.down'} size={12} tintColor={colors.ink3} />
      </Pressable>
      {open ? (
        <View style={styles.checkBody}>
          <Text style={[type.callout, { color: colors.ink }]}>{c.what}</Text>
          {c.note ? (
            <View style={[styles.noteChip, { backgroundColor: tone.soft }]}>
              <Text style={[type.footnote, { color: tone.ink }]}>{c.note}</Text>
            </View>
          ) : null}
          <Caption style={{ marginTop: 14 }}>Why it matters</Caption>
          <Text style={[type.callout, { color: colors.ink2, marginTop: 4 }]}>{c.why}</Text>
          {c.fix ? (
            <>
              <Caption style={{ marginTop: 14, color: colors.greenInk }}>Try this</Caption>
              <Text style={[type.callout, { color: colors.ink, marginTop: 4 }]}>{c.fix}</Text>
            </>
          ) : null}
          <View style={{ flexDirection: 'row', marginTop: 14 }}>
            <Button label="See it on your swing" icon="play.fill" kind="secondary" small onPress={onReplay} />
          </View>
        </View>
      ) : null}
    </View>
  );
}

function Row({ row, onPress }: { row: BreakdownRow; onPress?: () => void }) {
  const withheld = row.kind === 'withheld';
  return (
    <Pressable accessibilityRole={onPress ? 'button' : undefined} accessibilityLabel={`${row.name}: ${withheld ? 'Not measured' : row.label ?? ''}`}
      disabled={!onPress} onPress={onPress} style={({ pressed }) => [styles.row, withheld && styles.rowWithheld, pressed && { backgroundColor: colors.surface2 }]}>
      <View style={{ flex: 1 }}>
        <Text style={{ fontSize: 16.5, fontWeight: '500', color: colors.ink }}>{row.name}</Text>
        {withheld || row.kind === 'info' ? (
          <Text style={[type.footnote, { color: colors.ink3, marginTop: 3 }]}>{withheld ? row.reason : row.support}</Text>
        ) : null}
      </View>
      {withheld ? <StatusText label="Not measured" status="withheld" /> : row.kind === 'info' ? (
        <Text style={[type.footnote, { color: colors.ink3 }]}>{row.value}</Text>
      ) : (
        <StatusText label={row.label ?? ''} status={row.status} />
      )}
      {onPress ? <SymbolView name="play.fill" size={11} tintColor={colors.ink3} /> : null}
    </Pressable>
  );
}

const styles = themed(() => StyleSheet.create({
  screen: { flex: 1, backgroundColor: colors.bg },
  scroll: { paddingHorizontal: space.gutter, paddingBottom: 48 },
  top: { flexDirection: 'row', justifyContent: 'space-between', alignItems: 'center', paddingVertical: 14 },
  round: { width: 44, height: 44, borderRadius: 22, backgroundColor: colors.surface2, alignItems: 'center', justifyContent: 'center' },
  note: { flexDirection: 'row', gap: 8, alignItems: 'center', padding: 12, borderRadius: radius.md, marginBottom: 12, marginTop: 4 },
  club: { flexDirection: 'row', alignItems: 'center', gap: 8, alignSelf: 'flex-start', height: 36, paddingHorizontal: 14, borderRadius: radius.pill, borderWidth: 1, borderColor: colors.line, backgroundColor: colors.surface, marginBottom: 16 },
  exampleBar: { paddingTop: 10, paddingBottom: 6, gap: 2, borderTopWidth: 1, borderTopColor: colors.line, backgroundColor: colors.bg },
  exampleBadge: { height: 28, paddingHorizontal: 12, borderRadius: radius.pill, backgroundColor: colors.greenSoft, alignItems: 'center', justifyContent: 'center' },
  bar: { flexDirection: 'row', gap: 3, height: 8, marginTop: 16 },
  row: { flexDirection: 'row', alignItems: 'center', gap: 12, minHeight: 56, paddingVertical: 12, paddingHorizontal: 18, borderRadius: radius.md, backgroundColor: colors.surface, borderWidth: 1, borderColor: colors.line, marginBottom: 8 },
  rowWithheld: { backgroundColor: colors.surface2, borderStyle: 'dashed' },
  check: { borderRadius: radius.md, backgroundColor: colors.surface, borderWidth: 1, borderColor: colors.line, marginBottom: 8, overflow: 'hidden' },
  checkHead: { flexDirection: 'row', alignItems: 'center', gap: 12, minHeight: 64, paddingVertical: 12, paddingRight: 16, paddingLeft: 16 },
  checkRail: { width: 3, alignSelf: 'stretch', borderRadius: 2, marginVertical: 4 },
  noteChip: { marginTop: 10, paddingHorizontal: 12, paddingVertical: 8, borderRadius: radius.sm },
  checkBody: { paddingHorizontal: 20, paddingBottom: 16, paddingTop: 2, borderTopWidth: StyleSheet.hairlineWidth, borderTopColor: colors.line, marginHorizontal: 0 },
}));

