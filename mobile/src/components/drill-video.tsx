// A drill's demonstration video in YouTube's official player (IFrame Player API in a WebView: unmodified,
// YouTube's branding and links intact, the creator credited below).
//
// Privacy and YouTube API policy:
// * Nothing is requested from YouTube or Google until the golfer taps play: the poster is drawn locally (no
//   YouTube thumbnail), and the notice beside it says the video plays from YouTube under YouTube's Terms and
//   Google's Privacy Policy. Tapping play is the only action needed (no extra gate, YouTube Developer Policies
//   III.F.3), and it is the golfer's choice to load YouTube's player.
// * The player only exists while it is on screen: it is removed when the screen loses focus (another screen
//   pushed on top) or the app leaves the foreground, so nothing plays out of sight (III.I.9).
// * Links inside the player (YouTube logo, title, channel) are never disabled: they open outside the app.
// If the video can't play, the written steps stay and a "Watch on YouTube" link is offered.
import { useFocusEffect } from 'expo-router';
import { SymbolView } from 'expo-symbols';
import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { AppState, Linking, Pressable, StyleSheet, Text, useWindowDimensions, View } from 'react-native';
import { WebView } from 'react-native-webview';

import { PRIVACY_URL } from '@/config';
import { track } from '@/lib/analytics';
import {
  drillProps, duration, embedHtml, EMBED_ORIGIN, failureReason, frameSize, GOOGLE_PRIVACY_URL, parsePlayerEvent,
  READY_TIMEOUT_MS, YOUTUBE_TERMS_URL,
} from '@/lib/drill-video';
import type { DemoVideo, Drill } from '@/lib/types';
import { colors, radius, space, type, themed } from '@/theme';

type State = 'idle' | 'loading' | 'playing' | 'failed';

const openOutside = (url: string) => void Linking.openURL(url).catch(() => undefined);

export function DrillVideo({ video, drill }: { video: DemoVideo; drill: Drill }) {
  const [state, setState] = useState<State>('idle');
  const sent = useRef({ started: false, completed: false, failed: false });
  const html = useMemo(() => embedHtml(video), [video]);
  const stop = useCallback(() => setState((s) => (s === 'failed' ? s : 'idle')), []);
  // Never keep a player alive off screen: drop it on blur and when the app leaves the foreground.
  useFocusEffect(useCallback(() => stop, [stop]));
  useEffect(() => {
    const sub = AppState.addEventListener('change', (next) => { if (next !== 'active') stop(); });
    return () => sub.remove();
  }, [stop]);
  const fail = useCallback((code: string | null) => {
    setState('failed');
    if (!sent.current.failed) {
      sent.current.failed = true;
      track('drill_video_failed', { ...drillProps(drill), reason: failureReason(code) });
    }
  }, [drill]);
  // A player that never reports "ready" (blocked, offline, refused silently) must not leave a black frame.
  const ready = useRef(false);
  useEffect(() => {
    if (state !== 'loading') return;
    ready.current = false;
    const timer = setTimeout(() => { if (!ready.current) fail('api_timeout'); }, READY_TIMEOUT_MS);
    return () => clearTimeout(timer);
  }, [state, fail]);
  const { width: screenWidth } = useWindowDimensions();
  const size = frameSize(video.aspect, screenWidth - 2 * space.gutter);

  if (state === 'failed') {
    return (
      <View style={[styles.failed, { marginTop: 18 }]} accessibilityLiveRegion="polite">
        <SymbolView name="exclamationmark.triangle" size={20} tintColor={colors.ink2} />
        <View style={{ flex: 1 }}>
          <Text style={[type.callout, { color: colors.ink }]}>This video can&apos;t play here right now.</Text>
          <Text style={[type.footnote, { color: colors.ink2, marginTop: 2 }]}>The steps below show the whole drill.</Text>
          <Pressable accessibilityRole="link" onPress={() => openOutside(video.watch_url)} hitSlop={8} style={{ marginTop: 8, alignSelf: 'flex-start' }}>
            <Text style={[type.subhead, { color: colors.greenInk, fontWeight: '600' }]}>Watch on YouTube</Text>
          </Pressable>
        </View>
      </View>
    );
  }

  return (
    <View style={{ marginTop: 18 }}>
      <View style={[styles.frame, { width: size.width, height: size.height }]}>
        {state === 'idle' || !html ? (
          <Pressable accessibilityRole="button" accessibilityLabel={`Play video from YouTube: ${video.title}, by ${video.creator}`}
            accessibilityHint="Loads YouTube's video player" onPress={() => (html ? setState('loading') : fail('invalid_parameter'))}
            style={({ pressed }) => [StyleSheet.absoluteFill, styles.poster, pressed && { opacity: 0.92 }]}>
            <View style={styles.posterTop}>
              <SymbolView name="play.rectangle.fill" size={14} tintColor="rgba(255,255,255,0.85)" />
              <Text style={styles.posterLabel}>YouTube{video.duration_s ? ` · ${duration(video.duration_s)}` : ''}</Text>
            </View>
            <View style={styles.play}><SymbolView name="play.fill" size={26} tintColor="#fff" /></View>
            <View>
              <Text style={styles.posterTitle} numberOfLines={2}>{video.title}</Text>
              <Text style={styles.posterCreator} numberOfLines={1}>{video.creator}</Text>
            </View>
          </Pressable>
        ) : (
          <WebView
            source={{ html, baseUrl: EMBED_ORIGIN }}
            style={{ flex: 1, backgroundColor: '#000' }}
            originWhitelist={['*']}
            allowsInlineMediaPlayback
            mediaPlaybackRequiresUserAction={false}
            allowsFullscreenVideo
            javaScriptEnabled
            scrollEnabled={false}
            setSupportMultipleWindows={false}
            onShouldStartLoadWithRequest={(req) => {
              // The player's own frames load freely. The page itself never navigates: links in the player
              // (logo, title, channel) open outside the app, so they keep working without leaving Swing Guru.
              if (req.isTopFrame === false) return true;
              if (req.url === 'about:blank' || req.url.startsWith(EMBED_ORIGIN)) return true;
              if (/^https?:\/\//.test(req.url)) openOutside(req.url);
              return false;
            }}
            onMessage={(e) => {
              const ev = parsePlayerEvent(e.nativeEvent.data);
              if (!ev) return;
              if (ev.type === 'ready') ready.current = true;
              if (ev.type === 'playing') {
                ready.current = true;
                setState('playing');
                if (!sent.current.started) {
                  sent.current.started = true;
                  track('drill_video_started', drillProps(drill));
                }
              } else if (ev.type === 'ended' && !sent.current.completed) {
                sent.current.completed = true;
                track('drill_completed', drillProps(drill)); // demonstration watched to the end
              } else if (ev.type === 'error') {
                fail(ev.data);
              }
            }}
            onError={() => fail('load_failed')}
            onHttpError={(e) => { if (e.nativeEvent.url.includes('iframe_api')) fail('load_failed'); }}
          />
        )}
      </View>
      <Text style={[type.footnote, { color: colors.ink3, marginTop: 8 }]}>
        Video: {video.title} · {video.creator}. Plays from YouTube when you tap play. By playing, you agree to YouTube&apos;s{' '}
        <Text accessibilityRole="link" style={styles.link} onPress={() => openOutside(YOUTUBE_TERMS_URL)}>Terms of Service</Text>; Google&apos;s{' '}
        <Text accessibilityRole="link" style={styles.link} onPress={() => openOutside(GOOGLE_PRIVACY_URL)}>Privacy Policy</Text> and{' '}
        <Text accessibilityRole="link" style={styles.link} onPress={() => openOutside(PRIVACY_URL)}>ours</Text> apply.
      </Text>
    </View>
  );
}

/** A small, locally drawn mark for drill cards: tells the golfer the drill has a video (nothing loaded from YouTube). */
export function DrillThumb({ size = 56 }: { size?: number }) {
  return (
    <View style={[styles.thumb, { width: size, height: size }]} accessibilityLabel="Has a video">
      <SymbolView name="figure.golf" size={22} tintColor={colors.green} />
      <View style={styles.thumbPlay}><SymbolView name="play.fill" size={10} tintColor="#fff" /></View>
    </View>
  );
}

const styles = themed(() => StyleSheet.create({
  frame: { alignSelf: 'center', borderRadius: radius.lg, overflow: 'hidden', backgroundColor: '#000' },
  poster: { backgroundColor: colors.greenDeep, padding: 14, justifyContent: 'space-between' },
  posterTop: { flexDirection: 'row', alignItems: 'center', gap: 6 },
  posterLabel: { color: 'rgba(255,255,255,0.85)', fontSize: 12, fontWeight: '700', letterSpacing: 0.3 },
  posterTitle: { color: '#fff', fontSize: 15, fontWeight: '700', lineHeight: 19 },
  posterCreator: { color: 'rgba(255,255,255,0.75)', fontSize: 13, marginTop: 2 },
  play: { position: 'absolute', alignSelf: 'center', top: '50%', marginTop: -28, width: 56, height: 56, borderRadius: 28,
          backgroundColor: 'rgba(0,0,0,0.45)', alignItems: 'center', justifyContent: 'center', paddingLeft: 4 },
  failed: { flexDirection: 'row', gap: 12, padding: 16, borderRadius: radius.lg, backgroundColor: colors.surface2,
            borderWidth: 1, borderColor: colors.line },
  link: { color: colors.greenInk, fontWeight: '600' },
  thumb: { borderRadius: 16, backgroundColor: colors.greenSoft, alignItems: 'center', justifyContent: 'center' },
  thumbPlay: { position: 'absolute', right: 4, bottom: 4, width: 20, height: 20, borderRadius: 10, backgroundColor: colors.green,
               alignItems: 'center', justifyContent: 'center', paddingLeft: 2 },
}));
