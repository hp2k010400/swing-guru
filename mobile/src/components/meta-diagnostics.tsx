// Hidden Meta SDK diagnostics (Profile → long-press the version line). For testing attribution on a
// real iPhone: shows whether the SDK started, what each step returned, and lets the tester send one
// labelled test event. Shows no identifiers.
import { useState } from 'react';
import { Text, View } from 'react-native';

import { metaDiagnostics, runMetaTest, type MetaDiagnostics } from '@/lib/meta-ads';
import { colors, type } from '@/theme';

import { Button, Caption, Card } from './ui';

export function MetaDiagnosticsCard() {
  const [state, setState] = useState<MetaDiagnostics>(() => metaDiagnostics());
  const [busy, setBusy] = useState(false);
  return (
    <Card tone="flat" style={{ marginTop: 16 }}>
      <Caption>Meta SDK diagnostics</Caption>
      <Text selectable style={[type.footnote, { color: colors.ink2, marginTop: 8 }]}>
        App ID {state.appId ?? 'missing'} · client token {state.clientTokenConfigured ? 'configured' : 'MISSING'}
      </Text>
      <View style={{ marginTop: 8, gap: 4 }}>
        {state.steps.length === 0 ? (
          <Text style={[type.footnote, { color: colors.ink2 }]}>SDK not started yet.</Text>
        ) : state.steps.map((s, i) => (
          <Text key={`${s.at}-${i}`} selectable style={[type.footnote, { color: s.ok ? colors.ink : colors.problemInk }]}>
            {s.at} {s.ok ? '✓' : '✗'} {s.step}{s.detail ? ` — ${s.detail}` : ''}
          </Text>
        ))}
      </View>
      <View style={{ flexDirection: 'row', gap: 8, marginTop: 12 }}>
        <Button label="Run Meta test" small busy={busy} onPress={async () => {
          setBusy(true);
          try {
            setState(await runMetaTest());
          } finally {
            setBusy(false);
          }
        }} />
        <Button label="Refresh" kind="quiet" small onPress={() => setState(metaDiagnostics())} />
      </View>
    </Card>
  );
}
