// The last rank this account has seen celebrated, so a rank-up is celebrated once. Per account: a
// sign-in switch must not inherit another golfer's rank memory.
import { sessionIdentity } from './session';

function key(): string | null {
  const owner = sessionIdentity().owner;
  return owner ? `sg.rankSeen.${owner}` : 'sg.rankSeen.device';
}

export function lastSeenRank(): number | null {
  try {
    const raw = globalThis.localStorage?.getItem(key() ?? '');
    return raw === null || raw === undefined ? null : Number(raw);
  } catch {
    return null;
  }
}

export function rememberRank(index: number): void {
  try {
    globalThis.localStorage?.setItem(key() ?? '', String(index));
  } catch {
    // Best effort: the worst case is celebrating the same rank twice.
  }
}
