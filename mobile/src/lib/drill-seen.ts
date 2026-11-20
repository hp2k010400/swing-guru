// Which drills this account has opened on this device, so a return visit is recorded as `drill_revisited`.
// Per account (a sign-in switch must not inherit another golfer's history). Best effort.
import { sessionIdentity } from './session';

function key(): string {
  const owner = sessionIdentity().owner;
  return owner ? `sg.drillsSeen.${owner}` : 'sg.drillsSeen.device';
}

/** Records the visit; returns true when this drill had been opened before. */
export function markDrillSeen(id: string): boolean {
  try {
    const store = globalThis.localStorage;
    if (!store) return false;
    const seen = new Set<string>(JSON.parse(store.getItem(key()) ?? '[]'));
    const before = seen.has(id);
    seen.add(id);
    store.setItem(key(), JSON.stringify([...seen].slice(-100)));
    return before;
  } catch {
    return false;
  }
}
