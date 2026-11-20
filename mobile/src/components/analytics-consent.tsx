// The one-time analytics question (opt-in). 1.0.8: on the first welcome screen, and on Home until answered
// (golfers who skip the welcome). Profile can change it later. Nothing is sent before Allow.
import { useSyncExternalStore } from 'react';
import { StyleSheet, Text, View, type ViewStyle } from 'react-native';

import { analyticsAvailable, consent, setConsent, subscribeConsent } from '@/lib/analytics';
import { colors, radius, type, themed } from '@/theme';

import { Button, Caption } from './ui';

/** True while the analytics question is still unanswered (and analytics is configured). */
export function useConsentPending(): boolean {
  const answer = useSyncExternalStore(subscribeConsent, consent);
  return analyticsAvailable && answer === 'unset';
}

export function AnalyticsConsentCard({ style }: { style?: ViewStyle }) {
  if (!useConsentPending()) return null;
  return (
    <View style={[styles.card, style]}>
      <Caption>Help improve Swing Guru</Caption>
      <Text style={[type.callout, { color: colors.ink, marginTop: 8 }]}>
        Share anonymous usage data, such as which screens you use. Never your videos, measurements, name or email.
      </Text>
      <View style={{ flexDirection: 'row', gap: 8, marginTop: 12 }}>
        <Button label="Allow" small onPress={() => setConsent('granted')} />
        <Button label="Not now" kind="quiet" small onPress={() => setConsent('denied')} />
      </View>
    </View>
  );
}

const styles = themed(() => StyleSheet.create({
  card: { marginTop: 12, padding: 18, borderRadius: radius.lg, backgroundColor: colors.surface, borderWidth: 1, borderColor: colors.line },
}));
