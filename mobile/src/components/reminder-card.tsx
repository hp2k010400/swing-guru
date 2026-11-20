// "Practise, then re-check": on a fresh result of the golfer's own swing.
// Not asked yet → a card with "Remind me" (the iOS prompt appears only after that tap) or "No thanks".
// Already on → this analysis replaces the pending reminder, and a one-line confirmation shows.
import { SymbolView } from 'expo-symbols';
import { useEffect, useRef, useState } from 'react';
import { Pressable, Text, View } from 'react-native';

import { track } from '@/lib/analytics';
import { cancelFirstSwingReminder, disableReminders, enableReminders, reminderChoice, reminderPermission, scheduleReminder } from '@/lib/reminders';
import { REMIND_AFTER_DAYS, reminderDayLabel, shouldOfferReminder, shouldSchedule, type ReminderPermission } from '@/lib/reminders-core';
import { colors, type } from '@/theme';

import { Button, Caption, Card } from './ui';

type Props = { swingId: string; fresh: boolean; example: boolean; fix: string | null; club: string | null };

export function ReminderCard({ swingId, fresh, example, fix, club }: Props) {
  const [permission, setPermission] = useState<ReminderPermission | null>(null);
  const [choice, setChoice] = useState(reminderChoice);
  const [setFor, setSetFor] = useState<Date | null>(null);
  const [busy, setBusy] = useState(false);
  const scheduledFor = useRef<string | null>(null);
  const viewed = useRef(false);

  useEffect(() => {
    let alive = true;
    void reminderPermission().then((p) => alive && setPermission(p));
    return () => { alive = false; };
  }, []);

  // A real result of their own: the "film your first swing" reminder no longer applies.
  useEffect(() => {
    if (fresh && !example) void cancelFirstSwingReminder();
  }, [fresh, example]);

  // Reminders already on: count from this analysis (once per swing).
  useEffect(() => {
    if (!permission || scheduledFor.current === swingId) return;
    if (!shouldSchedule({ fresh, example, permission, choice })) return;
    scheduledFor.current = swingId;
    void scheduleReminder({ analysedAt: new Date(), fix, club }).then(setSetFor);
  }, [permission, choice, fresh, example, swingId, fix, club]);

  const offer = permission !== null && shouldOfferReminder({ fresh, example, permission, choice });
  useEffect(() => {
    if (offer && !viewed.current) {
      viewed.current = true;
      track('reminder_prompt_viewed', {});
    }
  }, [offer]);

  if (setFor) {
    return (
      <View style={{ flexDirection: 'row', alignItems: 'center', gap: 8, marginTop: 14 }}>
        <SymbolView name="bell.fill" size={14} tintColor={colors.gold} />
        <Text style={[type.footnote, { color: colors.ink2, flex: 1 }]}>
          We’ll remind you to re-check {reminderDayLabel(setFor, new Date())}.
        </Text>
      </View>
    );
  }
  if (!offer) return null;

  async function remind() {
    if (busy) return;
    setBusy(true);
    const on = await enableReminders('result');
    setChoice(on ? 'on' : 'off');
    setPermission(on ? 'granted' : await reminderPermission());
    setBusy(false);
  }

  async function noThanks() {
    await disableReminders('result');
    setChoice('off');
  }

  return (
    <Card style={{ marginTop: 16 }}>
      <Caption>Practise, then re-check</Caption>
      <Text style={[type.callout, { color: colors.ink, marginTop: 8 }]}>
        {fix
          ? `Work on your ${fix.toLowerCase()} this week. We'll remind you in ${REMIND_AFTER_DAYS} days to film again and see if your score went up.`
          : `We'll remind you in ${REMIND_AFTER_DAYS} days to film again and see if your score went up.`}
      </Text>
      <View style={{ flexDirection: 'row', alignItems: 'center', gap: 16, marginTop: 12 }}>
        <View style={{ flex: 1 }}>
          <Button label="Remind me" icon="bell" block onPress={() => void remind()} />
        </View>
        <Pressable accessibilityRole="button" onPress={() => void noThanks()} hitSlop={8}>
          <Text style={[type.subhead, { color: colors.ink2 }]}>No thanks</Text>
        </Pressable>
      </View>
    </Card>
  );
}
