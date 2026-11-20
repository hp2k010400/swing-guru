// First-launch onboarding: shown once, only to golfers with no swings yet (existing users never see it).
const KEY = 'sg.onboarding.v1';

/** Pure rule (tested): no analysed swings, nothing uploading, and not already seen or skipped. */
export function shouldShowOnboarding(swings: number | null, inFlight: number, seen: boolean): boolean {
  return swings === 0 && inFlight === 0 && !seen;
}

export function onboardingSeen(): boolean {
  try {
    return localStorage.getItem(KEY) !== null;
  } catch {
    return true; // storage unavailable: never nag on every launch
  }
}

export function markOnboardingSeen(how: 'completed' | 'skipped'): void {
  try {
    localStorage.setItem(KEY, how);
  } catch {
    /* a convenience only */
  }
}
