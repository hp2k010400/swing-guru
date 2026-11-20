// Trim: choose the part of the clip to analyse - from just before the set-up to after the finish.
// Only this selection is uploaded (lossless on-device cut), which keeps uploads small and analysis
// quick. The default selection is placed on the clip's biggest burst of motion: the swing.
import { useEvent, useEventListener } from 'expo';
import { Image } from 'expo-image';
import { router } from 'expo-router';
import { SymbolView } from 'expo-symbols';
import { useVideoPlayer, VideoView, type VideoPlayer, type VideoThumbnail } from 'expo-video';
import { useEffect, useState } from 'react';
import { ActivityIndicator, PanResponder, Pressable, StyleSheet, Text, View } from 'react-native';

import { Button, EmptyState, SafeArea } from '@/components/ui';
import type { PickedVideo } from '@/lib/analysis';
import { draft } from '@/lib/draft';
import { canTrim, exportTrim, findSwing, initialRange, suggestRange, TRIM_MAX_MS, TRIM_MIN_MS, type TrimRange } from '@/lib/trim';
import { colors, radius, type, themed } from '@/theme';

const THUMBS = 8;
const HANDLE = 24;
const STRIP = 60;

export default function TrimScreen() {
  const current = draft.get();
  if (!current) {
    return (
      <SafeArea style={styles.screen} edges={['top', 'bottom']}>
        <EmptyState icon="video.slash" title="No video selected" text="Choose a video to analyse."
          action={<Button label="Choose a video" onPress={() => router.replace('/analyze')} />} />
      </SafeArea>
    );
  }
  return <Trim video={current.video} />;
}

function Trim({ video }: { video: PickedVideo }) {
  const player = useVideoPlayer(video.uri, (p) => {
    p.muted = true;
    p.loop = false;
    p.timeUpdateEventInterval = 1 / 30;
  });
  const { isPlaying } = useEvent(player, 'playingChange', { isPlaying: player.playing });
  const [durationMs, setDurationMs] = useState(video.durationMs ?? 0);
  const [range, setRange] = useState<TrimRange>(() => initialRange(video.durationMs ?? 0));
  const [finding, setFinding] = useState<'busy' | 'found' | 'none'>(canTrim && video.durationMs ? 'busy' : 'none');
  const [thumbs, setThumbs] = useState<VideoThumbnail[]>([]);
  const [width, setWidth] = useState(0);
  const [positionMs, setPositionMs] = useState(0);
  // Mutable drag/playback bookkeeping read by event handlers (never during render).
  const [live] = useState(() => new Live(range));

  // Place the default selection on the swing (unless the golfer has already moved it).
  useEffect(() => {
    if (!canTrim || !video.durationMs) return;
    let alive = true;
    findSwing(video)
      .then((profile) => {
        if (!alive) return;
        const suggestion = live.touched ? null : suggestRange(profile, video.durationMs ?? profile.durationMs);
        if (suggestion) {
          live.set(suggestion);
          setRange(suggestion);
          seek(player, suggestion.startMs);
        }
        setFinding(suggestion ? 'found' : 'none');
      })
      .catch(() => alive && setFinding('none'));
    return () => {
      alive = false;
    };
  }, [video, player, live]);

  useEventListener(player, 'statusChange', ({ status }) => {
    if (status !== 'readyToPlay' || !live.askThumbs()) return;
    const total = video.durationMs || player.duration * 1000;
    if (!video.durationMs) {
      setDurationMs(total);
      live.set(initialRange(total));
      setRange(live.range);
    }
    seek(player, live.range.startMs);
    const times = Array.from({ length: THUMBS }, (_, i) => ((i + 0.5) * total) / THUMBS / 1000);
    player.generateThumbnailsAsync(times, { maxWidth: 160 }).then(setThumbs).catch(() => undefined);
  });

  // Playback stays inside the selection and loops it.
  useEventListener(player, 'timeUpdate', ({ currentTime }) => {
    const ms = currentTime * 1000;
    setPositionMs(ms);
    const r = live.range;
    if (player.playing && (ms >= r.endMs || ms < r.startMs - 250)) seek(player, r.startMs);
  });

  const inner = Math.max(1, width - HANDLE * 2);
  const pxPerMs = durationMs ? inner / durationMs : 0;
  const x = (ms: number) => HANDLE + ms * pxPerMs;
  const minLen = Math.min(TRIM_MIN_MS, durationMs);
  const maxLen = Math.min(TRIM_MAX_MS, durationMs);

  const drag = (kind: 'start' | 'end' | 'move') => ({
    begin() {
      live.touched = true;
      live.origin = live.range;
      player.pause();
    },
    move(dx: number) {
      if (!pxPerMs) return;
      const d = dx / pxPerMs;
      const o = live.origin;
      let next: TrimRange;
      if (kind === 'start') next = { startMs: clamp(o.startMs + d, Math.max(0, o.endMs - maxLen), o.endMs - minLen), endMs: o.endMs };
      else if (kind === 'end') next = { startMs: o.startMs, endMs: clamp(o.endMs + d, o.startMs + minLen, Math.min(durationMs, o.startMs + maxLen)) };
      else {
        const length = o.endMs - o.startMs;
        const start = clamp(o.startMs + d, 0, durationMs - length);
        next = { startMs: start, endMs: start + length };
      }
      live.set(next);
      setRange(next);
      seek(player, kind === 'end' ? next.endMs : next.startMs); // scrub to the edge being moved
    },
    end() {
      seek(player, live.range.startMs);
    },
  });
  const startPan = useDrag(drag('start'));
  const endPan = useDrag(drag('end'));
  const movePan = useDrag(drag('move'));

  const toggle = () => {
    if (player.playing) return player.pause();
    if (positionMs < range.startMs || positionMs >= range.endMs - 60) seek(player, range.startMs);
    player.play();
  };

  const next = () => {
    player.pause();
    const trimmed = exportTrim(video, range);
    trimmed.catch(() => undefined); // surfaced on the club screen
    draft.update({ range, trimmed });
    router.push('/club');
  };

  const cancel = () => {
    draft.clear();
    if (router.canGoBack()) router.back();
    else router.replace('/');
  };

  const length = range.endMs - range.startMs;
  const left = x(range.startMs);
  const right = x(range.endMs);
  const ready = durationMs > 0 && width > 0;

  return (
    <SafeArea style={styles.screen} edges={['top', 'bottom']}>
      <View style={styles.header}>
        <Pressable accessibilityRole="button" accessibilityLabel="Cancel" onPress={cancel} hitSlop={8} style={styles.round}>
          <SymbolView name="xmark" size={16} tintColor={colors.ink} />
        </Pressable>
        <Text style={[type.headline, { color: colors.ink }]}>Select your swing</Text>
        <View style={{ width: 44 }} />
      </View>

      <Pressable accessibilityRole="button" accessibilityLabel={isPlaying ? 'Pause' : 'Play selection'} onPress={toggle} style={styles.stage}>
        <VideoView player={player} style={StyleSheet.absoluteFill} contentFit="contain" nativeControls={false} />
        {!isPlaying ? (
          <View style={styles.play} pointerEvents="none">
            <SymbolView name="play.fill" size={26} tintColor="#fff" />
          </View>
        ) : null}
      </Pressable>

      <View style={styles.status}>
        {finding === 'busy' ? <ActivityIndicator size="small" color={colors.green} /> : null}
        <Text style={[type.subhead, { color: colors.ink }]}>
          {finding === 'busy' ? 'Finding your swing…' : finding === 'found' ? "We've selected your swing" : 'Drag the handles to your swing'}
        </Text>
      </View>
      <Text style={[type.footnote, { color: colors.ink2, textAlign: 'center', marginTop: 2 }]}>
        Start just before your set-up. End after your finish.
      </Text>

      <View style={styles.strip} onLayout={(e) => setWidth(e.nativeEvent.layout.width)}>
        <View style={[styles.thumbs, { left: HANDLE, right: HANDLE }]}>
          {thumbs.map((t) => (
            <Image key={t.requestedTime} source={t} style={{ flex: 1, height: STRIP }} contentFit="cover" />
          ))}
        </View>
        {ready ? (
          <>
            <View style={[styles.dim, { left: HANDLE, width: Math.max(0, left - HANDLE) }]} pointerEvents="none" />
            <View style={[styles.dim, { left: right, right: HANDLE }]} pointerEvents="none" />
            <View {...movePan} accessibilityLabel="Selected part of the video" style={[styles.frame, { left, width: right - left }]} />
            {positionMs >= range.startMs && positionMs <= range.endMs ? (
              <View pointerEvents="none" style={[styles.playhead, { left: x(positionMs) - 1 }]} />
            ) : null}
            <View {...startPan} accessibilityLabel="Start of selection" hitSlop={12} style={[styles.handle, styles.handleLeft, { left: left - HANDLE }]}>
              <View style={styles.grip} />
            </View>
            <View {...endPan} accessibilityLabel="End of selection" hitSlop={12} style={[styles.handle, styles.handleRight, { left: right }]}>
              <View style={styles.grip} />
            </View>
          </>
        ) : null}
      </View>

      <View style={styles.times}>
        <Text style={[type.footnote, { color: colors.ink3 }]}>{clock(range.startMs)}</Text>
        <Text style={[type.subhead, { color: colors.ink }]}>
          {(length / 1000).toFixed(1)} s selected · max {TRIM_MAX_MS / 1000} s
        </Text>
        <Text style={[type.footnote, { color: colors.ink3 }]}>{clock(range.endMs)}</Text>
      </View>
      {!canTrim ? (
        <Text style={[type.footnote, { color: colors.attentionInk, textAlign: 'center', marginTop: 6 }]}>
          Development build without the trimmer: the whole clip will be uploaded.
        </Text>
      ) : null}

      <View style={{ marginTop: 14 }}>
        <Button label="Next: choose club" block disabled={!ready || length < minLen} onPress={next} />
      </View>
    </SafeArea>
  );
}

class Live {
  origin: TrimRange;
  touched = false;
  thumbsAsked = false;
  constructor(public range: TrimRange) {
    this.origin = range;
  }
  set(range: TrimRange) {
    this.range = range;
  }
  /** True the first time only. */
  askThumbs() {
    const first = !this.thumbsAsked;
    this.thumbsAsked = true;
    return first;
  }
}

type DragHandlers = { begin: () => void; move: (dx: number) => void; end: () => void };

/** A PanResponder bound once, forwarding to the latest render's handlers. */
class Drag {
  handlers: DragHandlers | null = null;
  responder = PanResponder.create({
    onStartShouldSetPanResponder: () => true,
    onMoveShouldSetPanResponder: () => true,
    onPanResponderTerminationRequest: () => false,
    onPanResponderGrant: () => this.handlers?.begin(),
    onPanResponderMove: (_, gesture) => this.handlers?.move(gesture.dx),
    onPanResponderRelease: () => this.handlers?.end(),
    onPanResponderTerminate: () => this.handlers?.end(),
  });
  use(handlers: DragHandlers) {
    this.handlers = handlers;
  }
}

function useDrag(handlers: DragHandlers) {
  const [drag] = useState(() => new Drag());
  useEffect(() => {
    drag.use(handlers);
  });
  return drag.responder.panHandlers;
}

/** Seek outside render (the player is a native object, not React state). */
function seek(player: VideoPlayer, ms: number) {
  player.currentTime = ms / 1000;
}

function clamp(value: number, low: number, high: number) {
  return Math.min(Math.max(value, low), Math.max(low, high));
}

function clock(ms: number) {
  const s = Math.max(0, ms) / 1000;
  return `${Math.floor(s / 60)}:${(s % 60).toFixed(1).padStart(4, '0')}`;
}

const styles = themed(() => StyleSheet.create({
  screen: { flex: 1, backgroundColor: colors.bg, paddingHorizontal: 16 },
  header: { flexDirection: 'row', alignItems: 'center', justifyContent: 'space-between', paddingVertical: 10 },
  round: { width: 44, height: 44, borderRadius: 22, backgroundColor: colors.surface2, alignItems: 'center', justifyContent: 'center' },
  stage: { flex: 1, borderRadius: radius.lg, overflow: 'hidden', backgroundColor: '#000', marginTop: 4 },
  play: { position: 'absolute', alignSelf: 'center', top: '50%', marginTop: -32, width: 64, height: 64, borderRadius: 32, backgroundColor: 'rgba(0,0,0,0.45)', alignItems: 'center', justifyContent: 'center', paddingLeft: 4 },
  status: { flexDirection: 'row', alignItems: 'center', justifyContent: 'center', gap: 8, marginTop: 16, minHeight: 22 },
  strip: { height: STRIP, marginTop: 14 },
  thumbs: { position: 'absolute', top: 0, bottom: 0, flexDirection: 'row', borderRadius: 8, overflow: 'hidden', backgroundColor: colors.surface3 },
  dim: { position: 'absolute', top: 0, bottom: 0, backgroundColor: 'rgba(10,14,12,0.72)' },
  frame: { position: 'absolute', top: 0, bottom: 0, borderTopWidth: 3, borderBottomWidth: 3, borderColor: colors.green },
  playhead: { position: 'absolute', top: -4, bottom: -4, width: 2, borderRadius: 1, backgroundColor: colors.ink },
  handle: { position: 'absolute', top: 0, bottom: 0, width: HANDLE, backgroundColor: colors.green, alignItems: 'center', justifyContent: 'center' },
  handleLeft: { borderTopLeftRadius: 8, borderBottomLeftRadius: 8 },
  handleRight: { borderTopRightRadius: 8, borderBottomRightRadius: 8 },
  grip: { width: 3, height: 20, borderRadius: 2, backgroundColor: 'rgba(255,255,255,0.9)' },
  times: { flexDirection: 'row', justifyContent: 'space-between', alignItems: 'center', marginTop: 10 },
}));
