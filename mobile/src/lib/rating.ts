// Apple's own rating prompt (SKStoreReviewController via expo-store-review), shown after a good
// fresh result. Never from a button tap, never blocking: Apple decides whether it actually appears.
import * as StoreReview from 'expo-store-review';
import { Platform } from 'react-native';

import { track } from './analytics';
import { isGood, onFreshResult, parseState } from './rating-core';

const KEY = 'sg.rating';
const DELAY_MS = 2500; // let the score reveal finish first

/** Call once per fresh result (one swing id). Returns a cancel function for the pending prompt. */
export function maybeAskForRating(score: { total: number | null; scale: number; available: boolean }): () => void {
  if (Platform.OS !== 'ios') return () => undefined;
  let state;
  try {
    state = parseState(localStorage.getItem(KEY));
  } catch {
    return () => undefined;
  }
  const result = onFreshResult(state, isGood(score.total, score.scale, score.available), Date.now());
  try {
    localStorage.setItem(KEY, JSON.stringify(result.state));
  } catch {
    return () => undefined;
  }
  if (!result.ask) return () => undefined;
  const timer = setTimeout(() => {
    StoreReview.isAvailableAsync()
      .then((ok) => {
        if (!ok) return;
        track('rating_prompt_requested', {});
        return StoreReview.requestReview();
      })
      .catch(() => undefined);
  }, DELAY_MS);
  return () => clearTimeout(timer);
}
