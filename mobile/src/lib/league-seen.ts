// The league position this account last saw this week, so the app can show a small ▲/▼ when it changes.
// Per account and per week; best effort (no storage simply means no indicator).
import { sessionIdentity } from './session';

function key(week: string): string {
  return `sg.leaguePos.${sessionIdentity().owner ?? 'device'}.${week.slice(0, 10)}`;
}

/** Stores the current position and returns the previous one seen this week (null if none). */
export function rememberPosition(week: string, position: number): number | null {
  try {
    const store = globalThis.localStorage;
    if (!store) return null;
    const raw = store.getItem(key(week));
    store.setItem(key(week), String(position));
    return raw === null ? null : Number(raw);
  } catch {
    return null;
  }
}
