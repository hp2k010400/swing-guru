// Replay: the proven guided viewer (overlays, holds, per-check cards) in a WebView, loading this
// swing's stored analysis + replay video through short-lived signed URLs. Nothing is re-analysed.
// Opening is instant: the Summary's signed URLs are reused (no extra round trip) and the swing's
// thumbnail stays on screen until the viewer reports its first video frame ("ready").
// The viewer's back button returns to the Summary; the end of the walkthrough shows the swing's
// stored Swing Score as the payoff (never re-scored here).
import { Image } from 'expo-image';
import { router, useLocalSearchParams } from 'expo-router';
import { useCallback, useEffect, useMemo, useReducer, useState } from 'react';
import { SymbolView } from 'expo-symbols';
import { ActivityIndicator, Pressable, StyleSheet, Text, View } from 'react-native';
import { useSafeAreaInsets } from 'react-native-safe-area-context';
import { WebView } from 'react-native-webview';

import { ScoreRing } from '@/components/score';
import { Button, Caption, EmptyState, SafeArea } from '@/components/ui';
import { OVERLAY_VERSION } from '@/config';
import { track } from '@/lib/analytics';
import { api, cachedSwing, EXAMPLE_ID, friendly } from '@/lib/api';
import { createCloseGuard } from '@/lib/close-guard';
import { replayReducer } from '@/lib/processing-state';
import type { SwingDetail } from '@/lib/types';
import { colors, theme, type, themed } from '@/theme';

function viewerUri(swing: SwingDetail, check?: string) {
  const params = new URLSearchParams({ embed: '1', analysis: swing.replay.analysis_url, video: swing.replay.video_url });
  if (check) params.set('check', check);
  else params.set('autoplay', '1');
  // The replay overlay version this build asks for (config.ts): Build 12 (production profile) asks
  // for '2'; the staging profile asks for '3' (overlay v3, development). Unset = v1.
  if (OVERLAY_VERSION === '2' || OVERLAY_VERSION === '3') params.set('overlay', OVERLAY_VERSION);
  // 1.0.1: the replay follows the app's appearance and shows each card as one plain sentence.
  // Viewers that predate these parameters ignore them.
  params.set('theme', theme().scheme);
  params.set('cards', 'simple');
  return `${swing.replay.viewer_url}?${params.toString()}`;
}

export default function Replay() {
  const { id, check } = useLocalSearchParams<{ id: string; check?: string }>();
  const [swing, setSwing] = useState<SwingDetail | null>(() => cachedSwing(id));
  const [error, setError] = useState<string | null>(null);
  const [slow, setSlow] = useState(false);
  // run: bumping it reloads the viewer ("Watch again"); messages from an earlier run are ignored.
  // The loading X is absolutely positioned, so it must add the real top inset itself.
  const insets = useSafeAreaInsets();
  const [{ run, ready, ended }, dispatch] = useReducer(replayReducer, { run: 0, ready: false, ended: false });

  // Signed URLs expire: only fetch when the Summary's copy is missing or too old.
  const fetchReplay = useCallback(() => {
    (id === EXAMPLE_ID ? api.example() : api.swing(id)).then(setSwing).catch((e) => setError(friendly(e)));
  }, [id]);
  useEffect(() => {
    if (!swing) fetchReplay();
  }, [swing, fetchReplay]);
  useEffect(() => {
    track('replay_viewed', { check: check ? 'one' : 'all' });
  }, [check]);
  useEffect(() => {
    if (ready) return;
    const timer = setTimeout(() => setSlow(true), 6000);
    return () => clearTimeout(timer);
  }, [ready]);

  const retry = () => {
    setError(null);
    dispatch({ type: 'watchAgain' });
    setSwing(null);
  };
  // Guarded: a repeated close (double tap, or native X + viewer button) must not pop a second screen.
  const close = useMemo(() => createCloseGuard(() => (router.canGoBack() ? router.back() : router.replace({ pathname: '/swing/[id]', params: { id } }))), [id]);
  // "View Summary" always lands on *this* swing's Summary, even when the replay was opened directly.
  const toSummary = () => router.dismissTo({ pathname: '/swing/[id]', params: { id } });

  if (error) {
    return (
      <SafeArea style={styles.screen} edges={['top', 'bottom']}>
        <EmptyState icon="play.slash" title="Couldn't open the replay" text={error}
          action={<View style={{ gap: 10 }}><Button label="Try again" onPress={retry} /><Button label="Back" kind="quiet" onPress={close} /></View>} />
      </SafeArea>
    );
  }
  return (
    <SafeArea style={styles.screen} edges={['top', 'bottom']}>
      {swing ? (
        <WebView
          key={run}
          source={{ uri: viewerUri(swing, check) }}
          style={styles.web}
          originWhitelist={['https://*', 'http://*']}
          allowsInlineMediaPlayback
          mediaPlaybackRequiresUserAction={false}
          bounces={false}
          onMessage={(event) => {
            try {
              const message = JSON.parse(event.nativeEvent.data) as { source?: string; type?: string };
              if (message.source !== 'swing-viewer') return;
              if (message.type === 'close') close();
              else if (message.type) dispatch({ type: 'message', run, kind: message.type });
            } catch {
              /* ignore unrelated messages */
            }
          }}
          onHttpError={() => setError('The replay link expired. Please try again.')}
          onError={() => setError("You're offline. Check your connection and try again.")}
        />
      ) : null}
      {!ready ? (
        <View style={styles.loading} pointerEvents="box-none">
          {swing?.thumb_url ? <Image source={{ uri: swing.thumb_url }} style={StyleSheet.absoluteFill} contentFit="contain" /> : null}
          <Pressable accessibilityRole="button" accessibilityLabel="Close replay" onPress={close} hitSlop={8} style={[styles.close, { top: insets.top + 8 }]}>
            <SymbolView name="xmark" size={16} tintColor="#fff" />
          </Pressable>
          <View style={styles.badge} pointerEvents="none">
            <ActivityIndicator color="#fff" />
            <Text style={[type.subhead, { color: '#fff' }]}>{slow ? 'Still loading your replay…' : 'Loading replay…'}</Text>
          </View>
        </View>
      ) : null}
      {ended && swing ? <EndCard swing={swing} routeId={id} onSummary={toSummary} onAgain={() => { setSlow(false); dispatch({ type: 'watchAgain' }); }} /> : null}
    </SafeArea>
  );
}

/** The walkthrough's payoff: the stored score, then where to go next. */
function EndCard({ swing, routeId, onSummary, onAgain }: { swing: SwingDetail; routeId: string; onSummary: () => void; onAgain: () => void }) {
  const score = swing.score;
  const drill = swing.recommended[0]?.drill;
  return (
    <View style={styles.end}>
      <View style={styles.endCard}>
        <Caption style={{ textAlign: 'center', letterSpacing: 1.6 }}>Swing Score</Caption>
        {score.available && score.total !== null ? (
          <View style={{ alignItems: 'center', marginTop: 14 }}>
            <ScoreRing score={score.total} size={150} animate />
          </View>
        ) : (
          <Text style={[type.headline, { color: colors.ink, textAlign: 'center', marginTop: 12 }]}>{score.reason}</Text>
        )}
        <Text style={[type.subhead, { color: colors.ink2, textAlign: 'center', marginTop: 12 }]}>
          {score.measured} of {score.eligible} score checks measured
        </Text>
        <View style={{ gap: 6, marginTop: 20 }}>
          <Button label="View Summary" block onPress={onSummary} />
          {drill ? (
            <Button label={`Practise: ${drill.name}`} icon="target" kind="secondary" block
              onPress={() => router.dismissTo({ pathname: '/swing/[id]', params: { id: routeId, drill: drill.id } })} />
          ) : null}
          <Button label="Watch again" icon="arrow.counterclockwise" kind="quiet" block onPress={onAgain} />
        </View>
      </View>
    </View>
  );
}

const styles = themed(() => StyleSheet.create({
  screen: { flex: 1, backgroundColor: colors.bg },
  web: { flex: 1, backgroundColor: colors.bg },
  loading: { position: 'absolute', top: 0, left: 0, right: 0, bottom: 0, backgroundColor: '#0d1210', alignItems: 'center', justifyContent: 'center' },
  close: { position: 'absolute', left: 12, width: 44, height: 44, borderRadius: 22, backgroundColor: 'rgba(0,0,0,0.45)', alignItems: 'center', justifyContent: 'center' },
  end: { position: 'absolute', top: 0, left: 0, right: 0, bottom: 0, backgroundColor: 'rgba(13,18,16,0.55)', justifyContent: 'center', padding: 20 },
  endCard: { backgroundColor: colors.surface, borderRadius: 26, padding: 22 },
  badge: { flexDirection: 'row', alignItems: 'center', gap: 10, paddingHorizontal: 18, paddingVertical: 12, borderRadius: 999, backgroundColor: 'rgba(0,0,0,0.55)' },
}));
