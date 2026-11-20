// What the guided camera says, by phase. Pure, so the timing is tested.
// Recording starts before the set-up: the first seconds are for walking up and addressing the ball.

export const SETUP_SECONDS = 4;
/** The camera stops by itself after this (set-up + one swing + finish fit easily). */
export const MAX_SECONDS = 20;

export function recordingPrompt(recording: boolean, elapsed: number): { title: string; body: string } {
  if (!recording) {
    return {
      title: 'Line up with the guide',
      body: 'Phone at hip height, 3–4 m behind your hands. Fit head, feet and club inside the corners, then tap record.',
    };
  }
  if (elapsed < SETUP_SECONDS) {
    return { title: 'Recording. Walk up and set up now', body: 'Take your time over the ball.' };
  }
  return { title: 'Swing when you’re ready', body: 'Hold your finish, then tap stop.' };
}
