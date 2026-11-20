// When to show Apple's "Enjoying Swing Guru?" rating prompt. Pure, so it can be tested in Node.
//
// Asked only on a just-finished result that scored well, from the golfer's second good result on
// (not their very first analysis), and at most once every 120 days from our side. Apple adds its own
// cap (3 prompts a year per app) and may silently show nothing, which is fine.

export const GOOD_RATIO = 0.7;          // 7.0+ on the 10-point Swing Score
export const MIN_GOOD_RESULTS = 2;      // the golfer has seen it work at least twice
export const ASK_GAP_MS = 120 * 24 * 60 * 60 * 1000;

export interface RatingState {
  goodResults: number;
  lastAskedMs: number | null;
}

export const EMPTY_STATE: RatingState = { goodResults: 0, lastAskedMs: null };

export function isGood(total: number | null, scale: number, available: boolean): boolean {
  return available && total !== null && scale > 0 && total / scale >= GOOD_RATIO;
}

/** The state after a fresh result, and whether to ask now. */
export function onFreshResult(state: RatingState, good: boolean, now: number): { state: RatingState; ask: boolean } {
  if (!good) return { state, ask: false };
  const goodResults = state.goodResults + 1;
  const rested = state.lastAskedMs === null || now - state.lastAskedMs >= ASK_GAP_MS;
  const ask = goodResults >= MIN_GOOD_RESULTS && rested;
  return { state: { goodResults, lastAskedMs: ask ? now : state.lastAskedMs }, ask };
}

export function parseState(raw: string | null): RatingState {
  try {
    const value = JSON.parse(raw ?? 'null') as Partial<RatingState> | null;
    if (!value || typeof value !== 'object') return EMPTY_STATE;
    return {
      goodResults: Number.isFinite(value.goodResults) ? Number(value.goodResults) : 0,
      lastAskedMs: Number.isFinite(value.lastAskedMs) ? Number(value.lastAskedMs) : null,
    };
  } catch {
    return EMPTY_STATE;
  }
}
