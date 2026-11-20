// One close per screen: the replay's close can arrive twice (a double tap on the viewer's back
// button posts two "close" messages, or the native X and the viewer's button are both hit while the
// replay is loading). Each would call router.back(), and the second pops the screen *under* the
// replay too. The guard runs the first close and ignores repeats; if navigation did not happen
// (the screen is still mounted after `retryAfterMs`), a later tap works again.
export function createCloseGuard(navigate: () => void, now: () => number = Date.now, retryAfterMs = 1200) {
  let closedAt: number | null = null;
  return (): boolean => {
    const t = now();
    if (closedAt !== null && t - closedAt < retryAfterMs) return false;
    closedAt = t;
    navigate();
    return true;
  };
}
