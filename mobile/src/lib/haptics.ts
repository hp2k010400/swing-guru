// Restrained haptics, behind one gate (processing-state.ts) so polling, remounts and background
// returns never produce repeated vibration. Unsupported hardware or failures are ignored.
import * as Haptics from 'expo-haptics';

import { createHapticGate, type HapticKind } from './processing-state';

const gate = createHapticGate();

export function haptic(kind: HapticKind, key = ''): void {
  if (!gate.allow(kind, key)) return;
  const run = (): Promise<void> => {
    switch (kind) {
      case 'selection':
        return Haptics.selectionAsync();
      case 'stage':
        return Haptics.impactAsync(Haptics.ImpactFeedbackStyle.Soft);
      case 'success':
        return Haptics.notificationAsync(Haptics.NotificationFeedbackType.Success);
      case 'reveal':
        return Haptics.impactAsync(Haptics.ImpactFeedbackStyle.Light);
    }
  };
  try {
    run().catch(() => undefined);
  } catch {
    /* no haptic engine */
  }
}
