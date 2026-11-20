// Profile -> Send feedback: a category, a message, Submit. Presented as a sheet (below the status bar).
import { router } from 'expo-router';
import { SymbolView } from 'expo-symbols';
import { useState } from 'react';
import { Alert, Pressable, ScrollView, StyleSheet, Text, TextInput, View } from 'react-native';

import { Button, SafeArea } from '@/components/ui';
import { friendly } from '@/lib/api';
import { sendGeneralFeedback } from '@/lib/feedback';
import { FEEDBACK_CATEGORIES, MAX_MESSAGE, type FeedbackCategory } from '@/lib/feedback-core';
import { colors, radius, space, type, themed } from '@/theme';

export default function Feedback() {
  const [category, setCategory] = useState<FeedbackCategory | null>(null);
  const [message, setMessage] = useState('');
  const [busy, setBusy] = useState(false);
  const ready = Boolean(category && message.trim()) && !busy;

  async function submit() {
    if (!category || busy) return;
    setBusy(true);
    try {
      if (await sendGeneralFeedback(category, message)) {
        Alert.alert('Thanks for your feedback', 'We read every message.');
        router.back();
      }
    } catch (error) {
      Alert.alert("Couldn't send your feedback", friendly(error));
    } finally {
      setBusy(false);
    }
  }

  return (
    <SafeArea style={styles.screen} edges={['bottom']}>
      <View style={styles.header}>
        <Text style={[type.title2, { color: colors.ink }]}>Send feedback</Text>
        <Pressable accessibilityRole="button" accessibilityLabel="Close" hitSlop={8} onPress={() => router.back()} style={styles.round}>
          <SymbolView name="xmark" size={14} tintColor={colors.ink} />
        </Pressable>
      </View>
      <ScrollView contentContainerStyle={{ paddingBottom: 32 }} keyboardShouldPersistTaps="handled"
        automaticallyAdjustKeyboardInsets showsVerticalScrollIndicator={false}>
        <Text style={[type.footnote, { color: colors.ink2, marginBottom: 8 }]}>What is it about?</Text>
        <View style={styles.chips} accessibilityRole="radiogroup">
          {FEEDBACK_CATEGORIES.map((c) => {
            const on = c.id === category;
            return (
              <Pressable key={c.id} accessibilityRole="radio" accessibilityState={{ selected: on }}
                onPress={() => setCategory(c.id)} style={[styles.chip, on && styles.chipOn]}>
                <Text style={[type.subhead, { color: on ? colors.greenInk : colors.ink }]}>{c.label}</Text>
              </Pressable>
            );
          })}
        </View>
        <Text style={[type.footnote, { color: colors.ink2, marginTop: 18, marginBottom: 8 }]}>Message</Text>
        <TextInput value={message} onChangeText={setMessage} multiline maxLength={MAX_MESSAGE}
          placeholder="Tell us what happened or what you'd like to see" placeholderTextColor={colors.ink3}
          style={styles.input} accessibilityLabel="Message" />
        <View style={{ marginTop: 16 }}>
          <Button label="Submit" block busy={busy} disabled={!ready} onPress={() => void submit()} />
        </View>
        <Text style={[type.footnote, { color: colors.ink3, marginTop: 12 }]}>
          Sent with your account and app version so we can follow up. Please don&apos;t include passwords or payment details.
        </Text>
      </ScrollView>
    </SafeArea>
  );
}

const styles = themed(() => StyleSheet.create({
  screen: { flex: 1, backgroundColor: colors.bg, paddingHorizontal: space.gutter },
  header: { flexDirection: 'row', alignItems: 'center', justifyContent: 'space-between', paddingTop: 18, paddingBottom: 16 },
  round: { width: 44, height: 44, borderRadius: 22, backgroundColor: colors.surface2, alignItems: 'center', justifyContent: 'center' },
  chips: { flexDirection: 'row', flexWrap: 'wrap', gap: 8 },
  chip: { paddingHorizontal: 14, height: 40, justifyContent: 'center', borderRadius: radius.pill, borderWidth: 1,
    borderColor: colors.line, backgroundColor: colors.surface },
  chipOn: { borderColor: colors.green, backgroundColor: colors.greenTint },
  input: { minHeight: 140, maxHeight: 260, borderRadius: radius.md, borderWidth: 1, borderColor: colors.line, padding: 12,
    color: colors.ink, backgroundColor: colors.surface, textAlignVertical: 'top', fontSize: 16 },
}));
