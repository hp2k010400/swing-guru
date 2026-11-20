// Guided recording: the camera with a ghost golfer and frame to line up with (down the line, whole body and
// club in view), and recording that starts BEFORE the set-up, so the clip can't begin after address (the
// most common filming failure). Stops by itself after MAX_SECONDS. Hands the clip to Trim like a picked video.
//
// Camera permission is asked only here, when it's needed, after a one-button explainer (Apple: a custom
// screen before the system prompt may only say Continue, with no way to cancel past it).
import { CameraView, useCameraPermissions } from 'expo-camera';
import { File } from 'expo-file-system';
import { router } from 'expo-router';
import { SymbolView } from 'expo-symbols';
import { useEffect, useRef, useState } from 'react';
import { Alert, Linking, Pressable, StyleSheet, Text, View } from 'react-native';
import { useSafeAreaInsets } from 'react-native-safe-area-context';
import Svg, { Line, Path } from 'react-native-svg';

import { Golfer } from '@/components/filming-illustration';
import { Button } from '@/components/ui';
import { track } from '@/lib/analytics';
import { checkVideo, type PickedVideo } from '@/lib/analysis';
import { draft } from '@/lib/draft';
import { MAX_SECONDS, recordingPrompt } from '@/lib/recording';
import { colors, radius, space, type, themed } from '@/theme';

export default function Record() {
  const [permission, requestPermission] = useCameraPermissions();
  const insets = useSafeAreaInsets();
  const camera = useRef<CameraView>(null);
  const [ready, setReady] = useState(false);
  const [recording, setRecording] = useState(false);
  const [elapsed, setElapsed] = useState(0);
  const started = useRef(0);

  useEffect(() => {
    if (!recording) return;
    const timer = setInterval(() => setElapsed(Math.floor((Date.now() - started.current) / 1000)), 250);
    return () => clearInterval(timer);
  }, [recording]);

  if (!permission) return <View style={styles.black} />;

  if (!permission.granted) {
    const blocked = !permission.canAskAgain;
    return (
      <View style={[styles.screen, { paddingTop: insets.top + 40, paddingBottom: insets.bottom + 16 }]}>
        <View style={{ flex: 1, justifyContent: 'center', alignItems: 'center' }}>
          <View style={styles.orb}><SymbolView name="video.fill" size={38} tintColor={colors.gold} /></View>
          <Text style={[type.title1, { color: colors.ink, marginTop: 24, textAlign: 'center' }]}>Film your swing</Text>
          <Text style={[type.callout, { color: colors.ink2, marginTop: 10, textAlign: 'center' }]}>
            {blocked
              ? 'Camera access is turned off for Swing Guru. Turn it on in Settings to film here, or choose a video you already have.'
              : "Swing Guru uses your camera to record your swing so it can score it and show you what to fix. You'll line up with a guide first."}
          </Text>
        </View>
        {blocked ? (
          <View style={{ gap: 10 }}>
            <Button label="Open Settings" block onPress={() => void Linking.openSettings()} />
            <Button label="Choose a video instead" kind="quiet" block onPress={() => router.replace('/analyze')} />
          </View>
        ) : (
          <Button label="Continue" block onPress={() => void requestPermission()} />
        )}
      </View>
    );
  }

  async function start() {
    if (!camera.current || recording) return;
    track('recording_started', {});
    started.current = Date.now();
    setElapsed(0);
    setRecording(true);
    try {
      const result = await camera.current.recordAsync({ maxDuration: MAX_SECONDS });
      const durationMs = Date.now() - started.current;
      setRecording(false);
      if (!result?.uri) return;
      let size = 0;
      try {
        size = new File(result.uri).size ?? 0;
      } catch {
        size = 0;
      }
      const video: PickedVideo = { uri: result.uri, fileName: 'swing.mov', mimeType: 'video/quicktime', size, durationMs, longSide: 1920 };
      const problem = checkVideo(video);
      if (problem) {
        Alert.alert("This recording can't be analysed", problem);
        return;
      }
      track('recording_finished', { seconds: Math.round(durationMs / 1000) });
      draft.start(video);
      router.dismissAll();
      router.push('/trim');
    } catch {
      setRecording(false);
      Alert.alert("Couldn't record", 'Please try again, or choose a video from Photos.');
    }
  }

  const stop = () => camera.current?.stopRecording();
  const prompt = recordingPrompt(recording, elapsed);

  return (
    <View style={styles.black}>
      <CameraView ref={camera} style={StyleSheet.absoluteFill} facing="back" mode="video" videoQuality="1080p" mute
        onCameraReady={() => setReady(true)} />
      <GuideOverlay dim={recording} />

      <View style={[styles.top, { paddingTop: insets.top + 8 }]}>
        <Pressable accessibilityRole="button" accessibilityLabel="Close" disabled={recording} onPress={() => router.back()}
          hitSlop={8} style={[styles.round, recording && { opacity: 0 }]}>
          <SymbolView name="xmark" size={16} tintColor="#fff" />
        </Pressable>
        {recording ? (
          <View style={styles.rec}><View style={styles.recDot} /><Text style={styles.recText}>{elapsed}s</Text></View>
        ) : null}
        <View style={{ width: 44 }} />
      </View>

      <View style={[styles.bottom, { paddingBottom: insets.bottom + 18 }]}>
        <View style={styles.prompt}>
          <Text style={[type.headline, { color: '#fff', textAlign: 'center' }]}>{prompt.title}</Text>
          <Text style={[type.footnote, { color: 'rgba(255,255,255,0.8)', textAlign: 'center', marginTop: 4 }]}>{prompt.body}</Text>
        </View>
        <Pressable accessibilityRole="button" accessibilityLabel={recording ? 'Stop recording' : 'Start recording'}
          disabled={!ready} onPress={recording ? stop : () => void start()} style={[styles.shutter, !ready && { opacity: 0.4 }]}>
          <View style={recording ? styles.shutterStop : styles.shutterDot} />
        </Pressable>
      </View>
    </View>
  );
}

/** Where to stand: a frame for head-to-feet, a ghost right-handed golfer, and the target line away from camera. */
function GuideOverlay({ dim }: { dim: boolean }) {
  const gold = colors.gold;
  return (
    <View pointerEvents="none" style={[StyleSheet.absoluteFill, { opacity: dim ? 0.35 : 1 }]}>
      <Svg width="100%" height="100%" viewBox="0 0 100 178" preserveAspectRatio="xMidYMid meet">
        {/* head-to-feet frame corners */}
        <Path d="M14 30 v-8 h8 M86 30 v-8 h-8 M14 150 v8 h8 M86 150 v8 h-8" stroke={gold} strokeWidth={1.2} fill="none" strokeLinecap="round" />
        {/* target line running away from the camera */}
        <Line x1={63} y1={154} x2={61} y2={100} stroke={gold} strokeWidth={0.6} strokeDasharray="2 2" opacity={0.8} />
        <Golfer x={38} y={154} scale={1.05} tone="rgba(255,255,255,0.28)" />
      </Svg>
    </View>
  );
}

const styles = themed(() => StyleSheet.create({
  black: { flex: 1, backgroundColor: '#000' },
  screen: { flex: 1, backgroundColor: colors.bg, paddingHorizontal: space.gutter },
  orb: { width: 96, height: 96, borderRadius: 48, backgroundColor: colors.goldSoft, alignItems: 'center', justifyContent: 'center' },
  top: { position: 'absolute', left: 0, right: 0, top: 0, paddingHorizontal: space.gutter, flexDirection: 'row', alignItems: 'center', justifyContent: 'space-between' },
  round: { width: 44, height: 44, borderRadius: 22, backgroundColor: 'rgba(0,0,0,0.45)', alignItems: 'center', justifyContent: 'center' },
  rec: { flexDirection: 'row', alignItems: 'center', gap: 6, paddingHorizontal: 12, height: 30, borderRadius: radius.pill, backgroundColor: 'rgba(0,0,0,0.55)' },
  recDot: { width: 10, height: 10, borderRadius: 5, backgroundColor: '#e5484d' },
  recText: { color: '#fff', fontWeight: '700', fontVariant: ['tabular-nums'] },
  bottom: { position: 'absolute', left: 0, right: 0, bottom: 0, alignItems: 'center', paddingHorizontal: space.gutter, gap: 16 },
  prompt: { paddingHorizontal: 16, paddingVertical: 10, borderRadius: radius.lg, backgroundColor: 'rgba(0,0,0,0.55)', maxWidth: 360 },
  shutter: { width: 78, height: 78, borderRadius: 39, borderWidth: 4, borderColor: '#fff', alignItems: 'center', justifyContent: 'center' },
  shutterDot: { width: 60, height: 60, borderRadius: 30, backgroundColor: '#e5484d' },
  shutterStop: { width: 30, height: 30, borderRadius: 6, backgroundColor: '#e5484d' },
}));
