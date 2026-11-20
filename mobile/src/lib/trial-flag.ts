// This device saw a server-verified free-trial purchase (any paywall). Used so the post-first-score trial
// offer is never shown to someone who already started a trial (1.0.7). The App Store's own trial
// eligibility covers other devices and reinstalls (lib/first-result.ts trialAlreadyStarted).
const KEY = 'sg.trial.started';

export function markTrialStarted(): void {
  try {
    localStorage.setItem(KEY, new Date().toISOString());
  } catch {
    /* the App Store eligibility check still applies */
  }
}

export function trialRecordedHere(): boolean {
  try {
    return localStorage.getItem(KEY) !== null;
  } catch {
    return false;
  }
}
