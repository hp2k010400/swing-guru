// "Was this analysis helpful?" - a quiet card at the end of the Summary, never a modal.
import { SymbolView } from 'expo-symbols';
import { useState } from 'react';
import { Pressable, StyleSheet, Text, TextInput, View } from 'react-native';

import { friendly } from '@/lib/api';
import { rememberRating, sendAnalysisFeedback, sentRating } from '@/lib/feedback';
import { MAX_MESSAGE } from '@/lib/feedback-core';
import { colors, radius, type, themed } from '@/theme';

import { Button } from './ui';

export function AnalysisFeedback({ swingId, measuredChecks }: { swingId: string; measuredChecks: string[] }) {
  const [rating, setRating] = useState<'up' | 'down' | null>(() => sentRating(swingId));
  const [stage, setStage] = useState<'ask' | 'comment' | 'thanks'>(() => (sentRating(swingId) ? 'thanks' : 'ask'));
  const [comment, setComment] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function send(choice: 'up' | 'down', text = '', thenThanks = true) {
    setBusy(true);
    setError(null);
    try {
      await sendAnalysisFeedback(swingId, choice, text, measuredChecks);
      rememberRating(swingId, choice);
      if (thenThanks) setStage('thanks');
    } catch (e) {
      setError(friendly(e));
    } finally {
      setBusy(false);
    }
  }

  function choose(choice: 'up' | 'down') {
    if (busy) return;
    setRating(choice);
    if (choice === 'up') {
      void send('up');
    } else {
      // Record the thumbs down straight away; an optional comment then updates the same record.
      setStage('comment');
      void send('down', '', false);
    }
  }

  if (stage === 'thanks' && rating) {
    return (
      <View style={styles.card} accessibilityLiveRegion="polite">
        <Text style={[type.footnote, { color: colors.ink2 }]}>Thanks for the feedback. It helps us improve Swing Guru.</Text>
      </View>
    );
  }
  return (
    <View style={styles.card}>
      <View style={styles.row}>
        <Text style={[type.subhead, { color: colors.ink, flex: 1 }]}>Was this analysis helpful?</Text>
        {(['up', 'down'] as const).map((choice) => {
          const on = rating === choice;
          const icon = choice === 'up'
            ? (on ? 'hand.thumbsup.fill' : 'hand.thumbsup')
            : (on ? 'hand.thumbsdown.fill' : 'hand.thumbsdown');
          return (
            <Pressable key={choice} accessibilityRole="button" disabled={busy} hitSlop={6}
              accessibilityLabel={choice === 'up' ? 'Yes, helpful' : 'No, not helpful'}
              accessibilityState={{ selected: on }} onPress={() => choose(choice)}
              style={[styles.thumb, on && styles.thumbOn]}>
              <SymbolView name={icon} size={18} tintColor={on ? colors.green : colors.ink2} />
            </Pressable>
          );
        })}
      </View>
      {stage === 'comment' ? (
        <View style={{ marginTop: 12 }}>
          <Text style={[type.footnote, { color: colors.ink2, marginBottom: 6 }]}>What seemed wrong? (optional)</Text>
          <TextInput value={comment} onChangeText={setComment} multiline maxLength={MAX_MESSAGE}
            placeholder="For example: the head check looked wrong" placeholderTextColor={colors.ink3}
            style={styles.input} accessibilityLabel="What seemed wrong? Optional" />
          <View style={{ flexDirection: 'row', gap: 8, marginTop: 10 }}>
            <Button label="Send" small busy={busy} disabled={busy || !comment.trim()}
              onPress={() => void send('down', comment)} />
            <Button label="Skip" kind="quiet" small disabled={busy} onPress={() => setStage('thanks')} />
          </View>
        </View>
      ) : null}
      {error ? <Text style={[type.footnote, { color: colors.problemInk, marginTop: 8 }]}>{error}</Text> : null}
    </View>
  );
}

const styles = themed(() => StyleSheet.create({
  card: { marginTop: 22, padding: 14, borderRadius: radius.lg, borderWidth: 1, borderColor: colors.line, backgroundColor: colors.surface },
  row: { flexDirection: 'row', alignItems: 'center', gap: 8 },
  thumb: { width: 44, height: 44, borderRadius: 22, alignItems: 'center', justifyContent: 'center', backgroundColor: colors.surface2 },
  thumbOn: { backgroundColor: colors.greenSoft },
  input: { minHeight: 72, maxHeight: 140, borderRadius: radius.md, borderWidth: 1, borderColor: colors.line, padding: 10,
    color: colors.ink, backgroundColor: colors.bg, textAlignVertical: 'top', fontSize: 15 },
}));
