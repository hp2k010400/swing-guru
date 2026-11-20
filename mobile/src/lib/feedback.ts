// Sending feedback, plus a per-device note of which rating was already sent for a swing.
import * as Application from 'expo-application';

import { api } from './api';
import { analysisFeedback, generalFeedback, versionLabel, type FeedbackCategory } from './feedback-core';

const appVersion = () => versionLabel(Application.nativeApplicationVersion, Application.nativeBuildVersion);

export const sendAnalysisFeedback = (swingId: string, rating: 'up' | 'down', comment: string, measured: string[]) =>
  api.feedback(analysisFeedback(swingId, rating, comment, measured, appVersion()));

export async function sendGeneralFeedback(category: FeedbackCategory, message: string): Promise<boolean> {
  const body = generalFeedback(category, message, appVersion());
  if (!body) return false;
  await api.feedback(body);
  return true;
}

// A convenience so the card shows "Thanks" on a revisit; the server keeps the real record.
const key = (swingId: string) => `swingguru.feedback.${swingId}`;

export function sentRating(swingId: string): 'up' | 'down' | null {
  try {
    const value = localStorage.getItem(key(swingId));
    return value === 'up' || value === 'down' ? value : null;
  } catch {
    return null;
  }
}

export function rememberRating(swingId: string, rating: 'up' | 'down'): void {
  try {
    localStorage.setItem(key(swingId), rating);
  } catch {
    /* a convenience only */
  }
}
