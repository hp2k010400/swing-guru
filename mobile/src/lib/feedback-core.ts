// Feedback payloads (pure, testable). The server reads the swing's score context itself; the app adds
// only which checks were shown as measured and its own version. No video, no device identifiers.
export type FeedbackCategory = 'swing_analysis' | 'bug' | 'feature' | 'other';

export const FEEDBACK_CATEGORIES: { id: FeedbackCategory; label: string }[] = [
  { id: 'swing_analysis', label: 'Swing analysis' },
  { id: 'bug', label: 'Bug' },
  { id: 'feature', label: 'Feature request' },
  { id: 'other', label: 'Other' },
];

export const MAX_MESSAGE = 2000;

export function analysisFeedback(swingId: string, rating: 'up' | 'down', comment: string,
                                 measuredChecks: string[], appVersion: string | null) {
  const message = rating === 'down' ? comment.trim().slice(0, MAX_MESSAGE) : '';
  return {
    kind: 'analysis' as const, swing_id: swingId, rating,
    ...(message ? { message } : {}),
    measured_checks: measuredChecks.slice(0, 12),
    ...(appVersion ? { app_version: appVersion.slice(0, 40) } : {}),
  };
}

export function generalFeedback(category: FeedbackCategory, message: string, appVersion: string | null) {
  const text = message.trim();
  if (!text) return null;
  return {
    kind: 'general' as const, category, message: text.slice(0, MAX_MESSAGE),
    ...(appVersion ? { app_version: appVersion.slice(0, 40) } : {}),
  };
}

/** "0.1.0 (9)" from the native version and build number, or null when unknown. */
export function versionLabel(version: string | null | undefined, build: string | null | undefined): string | null {
  if (!version) return null;
  return build ? `${version} (${build})` : version;
}
