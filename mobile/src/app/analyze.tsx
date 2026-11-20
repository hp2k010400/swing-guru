// Analyze sheet: record with the camera or choose an existing video, then hand off to Trim -> Club.
import { router } from 'expo-router';
import { SymbolView, type SymbolViewProps } from 'expo-symbols';
import { useState } from 'react';
import { ActivityIndicator, Alert, Pressable, StyleSheet, Text, View } from 'react-native';

import { IS_STAGING } from '@/config';
import { ApiError, friendly } from '@/lib/api';
import { checkVideo, pickVideo } from '@/lib/analysis';
import { draft } from '@/lib/draft';
import { colors, radius, type, themed } from '@/theme';

// 1.0.1: the two added tips come from production failures on 2026-10-13: clips that started after the
// set-up (no_setup) and, on the evidence of a reproduction, slow-motion clips (no swing found).
const TIPS: [SymbolViewProps['name'], string][] = [
  ['arrow.up.and.down.and.arrow.left.and.right', 'Down the line: behind your hands, looking along the target line'],
  ['figure.golf', 'Whole body and club in frame, with space above your head'],
  ['iphone', 'Phone propped up and still, at hip height, 3–4 m away'],
  ['timer', 'Start recording 2 seconds before you set up. Stop after your finish.'],
  ['film', 'One swing per video, at normal speed: no slow motion and no edits'],
  ['speedometer', '60 fps if you can (Camera app → Settings → Record Video). 30 fps works too.'],
];

function pickerFailure(error: unknown): { title: string; body: string } {
  if (error instanceof ApiError) return { title: "Couldn't open the video", body: friendly(error) };
  const text = `${(error as { code?: string })?.code ?? ''} ${(error as Error)?.message ?? ''}`.toLowerCase();
  if (/icloud|network|download|cloud|phphotos|-3164|3164/.test(text)) {
    return { title: 'Video still in iCloud', body: 'This video needs to download from iCloud first. Open it in the Photos app, wait for it to load, then try again.' };
  }
  if (/export|transcod|avfoundation|unsupported|codec|format/.test(text)) {
    return { title: "Couldn't read this video", body: "This video's format couldn't be read. Try a video recorded with the iPhone Camera app, or save the video to Photos again and retry." };
  }
  return { title: "Couldn't open the video", body: 'Please try again. If it keeps happening, try a video recorded with the iPhone Camera app.' };
}

/** Test builds only: the error's code and first line, never a file path. */
function technical(error: unknown): string {
  const code = (error as { code?: string })?.code ?? 'no code';
  const message = String((error as Error)?.message ?? error).split('\n')[0].replace(/(file|ph|assets-library):\/\/\S+/gi, '<file>');
  return `${code}: ${message}`.slice(0, 160);
}

export default function Analyze() {
  const [busy, setBusy] = useState<null | 'camera' | 'library'>(null);

  async function go(source: 'camera' | 'library') {
    setBusy(source);
    try {
      const video = await pickVideo(source);
      if (!video) return;
      const problem = checkVideo(video);
      if (problem) {
        Alert.alert("This video can't be analysed", problem);
        return;
      }
      draft.start(video);
      router.dismissAll();
      router.push('/trim');
    } catch (error) {
      // The real reason used to be replaced by a generic "Something went wrong" (Build 16). Picker and
      // file errors happen before any upload, on the phone; say what is known, and in test builds add
      // the technical code so a failure can be diagnosed (no paths, tokens or video content).
      const { title, body } = pickerFailure(error);
      Alert.alert(title, IS_STAGING ? `${body}\n\n(${technical(error)})` : body);
    } finally {
      setBusy(null);
    }
  }

  return (
    <View style={styles.sheet}>
      <Text style={[type.title2, { color: colors.ink }]}>Analyze a swing</Text>
      <Text style={[type.callout, { color: colors.ink2, marginTop: 6 }]}>Next you&apos;ll select the swing and your club.</Text>

      <Option primary icon="video.fill" title="Record a swing" detail="Line up with the guide, then record" busy={busy === 'camera'}
        onPress={() => { router.dismissAll(); router.push('/record'); }} />
      <Option icon="photo.on.rectangle" title="Choose a video" detail="From Photos · the original file, not a chat copy" busy={busy === 'library'} onPress={() => go('library')} />

      <View style={styles.tips}>
        {TIPS.map(([icon, text]) => (
          <View key={text} style={styles.tip}>
            <SymbolView name={icon} size={17} tintColor={colors.green} />
            <Text style={[type.footnote, { color: colors.ink2, flex: 1 }]}>{text}</Text>
          </View>
        ))}
      </View>
    </View>
  );
}

function Option({ icon, title, detail, onPress, primary = false, busy = false }: {
  icon: SymbolViewProps['name'];
  title: string;
  detail: string;
  onPress: () => void;
  primary?: boolean;
  busy?: boolean;
}) {
  const fg = primary ? '#fff' : colors.ink;
  return (
    <Pressable accessibilityRole="button" accessibilityLabel={title} onPress={onPress} disabled={busy}
      style={({ pressed }) => [styles.option, primary && styles.optionPrimary, pressed && { opacity: 0.85 }]}>
      <View style={[styles.optionIcon, primary && { backgroundColor: 'rgba(255,255,255,0.16)' }]}>
        {busy ? <ActivityIndicator color={primary ? '#fff' : colors.green} /> : <SymbolView name={icon} size={22} tintColor={primary ? '#fff' : colors.green} />}
      </View>
      <View style={{ flex: 1 }}>
        <Text style={[type.headline, { color: fg }]}>{title}</Text>
        <Text style={[type.footnote, { color: primary ? 'rgba(255,255,255,0.8)' : colors.ink2, marginTop: 2 }]}>{detail}</Text>
      </View>
      <SymbolView name="chevron.right" size={15} tintColor={primary ? 'rgba(255,255,255,0.7)' : colors.ink3} />
    </Pressable>
  );
}

const styles = themed(() => StyleSheet.create({
  sheet: { flex: 1, backgroundColor: colors.surface, padding: 22, paddingTop: 26 },
  option: { flexDirection: 'row', alignItems: 'center', gap: 14, padding: 16, borderRadius: radius.lg, borderWidth: 1, borderColor: colors.line, backgroundColor: colors.surface, marginTop: 12 },
  optionPrimary: { backgroundColor: colors.green, borderColor: colors.green, marginTop: 20 },
  optionIcon: { width: 46, height: 46, borderRadius: 14, backgroundColor: colors.greenSoft, alignItems: 'center', justifyContent: 'center' },
  tips: { marginTop: 22, gap: 10 },
  tip: { flexDirection: 'row', alignItems: 'center', gap: 12 },
}));
