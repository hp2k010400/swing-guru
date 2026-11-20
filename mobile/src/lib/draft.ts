// The swing being set up (picked -> trimmed -> club chosen) before it becomes an analysis job.
// In memory only: if the app is killed mid-setup the golfer simply picks the video again.
import type { PickedVideo } from './analysis';
import type { TrimRange } from './trim';

export interface Draft {
  video: PickedVideo;
  range: TrimRange | null;
  /** Started when the golfer leaves the trim screen, so it is usually done before a club is chosen. */
  trimmed: Promise<PickedVideo> | null;
}

let current: Draft | null = null;

export const draft = {
  get: () => current,
  start(video: PickedVideo) {
    current = { video, range: null, trimmed: null };
  },
  update(patch: Partial<Draft>) {
    if (current) current = { ...current, ...patch };
  },
  clear() {
    current = null;
  },
};
