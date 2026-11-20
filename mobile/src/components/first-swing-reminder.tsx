// "Remind me at my next practice": for golfers who finished onboarding or watched the example but haven't
// analysed a swing of their own yet. The iOS prompt appears only after the tap; one local reminder,
// 2 days later at 6 pm. Shown on the example result and on the empty Home screen.
import { SymbolView } from 'expo-symbols';
import { useEffect, useRef, useState } from 'react';
import { Pressable, Text, View } from 'react-native';

import { track } from '@/lib/analytics';
import { declineFirstSwing, firstSwingChoice, reminderChoice, reminderPermission, remindFirstSwing } from '@/lib/reminders';
import { reminderDayLabel, shouldOfferFirstSwingReminder, type ReminderPermission } from '@/lib/reminders-core';
import { colors, type } from '@/theme';

import { Button, Caption, Card } from './ui';

type Props = { from: 'example' | 'home'; ownSwings: number | null };

export function FirstSwingReminderCard({ from, ownSwings }: Props) {
  const [permission, setPermission] = useState<ReminderPermission | null>(null);
  const [firstChoice, setFirstChoice] = useState(firstSwingChoice);
  const [setFor, setSetFor] = useState<Date | null>(null);
  const [busy, setBusy] = useState(false);
  const viewed = useRef(false);

  useEffect(() => {
    let alive = true;
    void reminderPermission().then((p) => alive && setPermission(p));
    return () => { alive = false; };
  }, []);

  const offer = permission !== null
    && shouldOfferFirstSwingReminder({ ownSwings, permission, choice: reminderChoice(), firstChoice });
  useEffect(() => {
    if (offer && !viewed.current) {
      viewed.current = true;
      track('reminder_prompt_viewed', { kind: 'first_swing', from });
    }
  }, [offer, from]);

  if (setFor) {
    return (
      <View style={{ flexDirection: 'row', alignItems: 'center', gap: 8, marginTop: 14 }}>
        <SymbolView name="bell.fill" size={14} tintColor={colors.gold} />
        <Text style={[type.footnote, { color: colors.ink2, flex: 1 }]}>
          {(() => {
            const day = reminderDayLabel(setFor, new Date());
            return day === 'today' ? 'We’ll remind you later today.' : `We’ll remind you ${day} evening.`;
          })()}
        </Text>
      </View>
    );
  }
  if (!offer) return null;

  async function remind() {
    if (busy) return;
    setBusy(true);
    const when = await remindFirstSwing(from);
    setSetFor(when);
    setFirstChoice(firstSwingChoice());
    if (!when) setPermission(await reminderPermission());
    setBusy(false);
  }

  function notNow() {
    declineFirstSwing(from);
    setFirstChoice('declined');
  }

  return (
    <Card style={{ marginTop: 16 }}>
      <Caption>Not at the range right now?</Caption>
      <Text style={[type.callout, { color: colors.ink, marginTop: 8 }]}>
        We’ll send one reminder in a couple of days to film a swing at your next practice.
      </Text>
      <View style={{ gap: 10, marginTop: 12, alignItems: 'center' }}>
        <Button label="Remind me at my next practice" icon="bell" kind="secondary" block busy={busy} onPress={() => void remind()} />
        <Pressable accessibilityRole="button" onPress={notNow} hitSlop={8}>
          <Text style={[type.subhead, { color: colors.ink2 }]}>Not now</Text>
        </Pressable>
      </View>
    </Card>
  );
}
