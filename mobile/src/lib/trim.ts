// Choosing the part of the clip to analyse. Only that part is uploaded (lossless on-device trim).
import * as SwingTrim from '../../modules/swing-trim';

import type { PickedVideo } from './analysis';

/** The analysis needs a still set-up before Address and a settled finish, so keep room either side. */
export const TRIM_MIN_MS = 2_000;
/** Above this a trimmed clip is re-encoded to 1080p (headroom under the 50 MB upload limit). */
export const FIT_THRESHOLD_BYTES = 45 * 1024 * 1024;
/**
 * Above this long side (px) a trimmed clip is re-encoded to 1080p whatever its size: short 4K clips
 * fit the upload limit but most failed analysis on the server (12-13 Nov 2026: 9 of 10 4K failed).
 */
export const FIT_MAX_LONG_SIDE = 1920;

/** Whether a trimmed clip should be re-encoded to 1080p before upload. */
export function needsFit(sizeBytes: number, longSide: number | undefined): boolean {
  return sizeBytes > FIT_THRESHOLD_BYTES || (longSide ?? 0) > FIT_MAX_LONG_SIDE;
}
export const TRIM_MAX_MS = 10_000;
const BEFORE_PEAK_S = 3.5; // set-up stillness + takeaway + backswing before the fastest motion
const AFTER_PEAK_S = 2.5; // follow-through and a held finish after it

export interface TrimRange {
  startMs: number;
  endMs: number;
}

export function clampRange(range: TrimRange, durationMs: number): TrimRange {
  let { startMs, endMs } = range;
  startMs = Math.max(0, Math.min(startMs, durationMs - Math.min(TRIM_MIN_MS, durationMs)));
  endMs = Math.min(durationMs, Math.max(endMs, startMs + Math.min(TRIM_MIN_MS, durationMs)));
  if (endMs - startMs > TRIM_MAX_MS) endMs = startMs + TRIM_MAX_MS;
  return { startMs, endMs };
}

/** Before the motion profile arrives: the whole clip when it fits, otherwise its middle. */
export function initialRange(durationMs: number): TrimRange {
  if (durationMs <= TRIM_MAX_MS) return { startMs: 0, endMs: durationMs };
  const middle = durationMs / 2;
  return clampRange({ startMs: middle - TRIM_MAX_MS / 2, endMs: middle + TRIM_MAX_MS / 2 }, durationMs);
}

/**
 * The swing is the clip's biggest one-second burst of motion. The first and last half second are
 * ignored (tapping record/stop shakes the phone). Returns null when nothing stands out, so the
 * screen keeps the neutral default instead of guessing.
 */
export function suggestRange(profile: SwingTrim.MotionProfile, durationMs: number): TrimRange | null {
  const { values, samplesPerSecond: rate } = profile;
  const window = Math.max(1, Math.round(rate));
  const edge = Math.ceil(rate * 0.5);
  const sums: { index: number; sum: number }[] = [];
  for (let i = edge; i + window <= values.length - edge; i++) {
    let sum = 0;
    for (let j = i; j < i + window; j++) sum += values[j];
    sums.push({ index: i, sum });
  }
  if (sums.length < 2) return null;
  const sorted = sums.map((s) => s.sum).sort((a, b) => a - b);
  const median = sorted[Math.floor(sorted.length / 2)];
  const best = sums.reduce((a, b) => (b.sum > a.sum ? b : a));
  if (best.sum < Math.max(median * 2, 1)) return null;
  const peakS = (best.index + window / 2) / rate;
  return clampRange({ startMs: (peakS - BEFORE_PEAK_S) * 1000, endMs: (peakS + AFTER_PEAK_S) * 1000 }, durationMs);
}

export const canTrim = SwingTrim.available;

export function findSwing(video: PickedVideo): Promise<SwingTrim.MotionProfile> {
  const seconds = (video.durationMs ?? 0) / 1000;
  return SwingTrim.motionProfile(video.uri, seconds > 40 ? 4 : 8);
}

/** Cut the selection out of the original without re-encoding (same frame rate and quality). */
export async function exportTrim(video: PickedVideo, range: TrimRange): Promise<PickedVideo> {
  if (!SwingTrim.available) return video; // Expo Go / web development builds only
  let out = await SwingTrim.trim(video.uri, range.startMs, range.endMs);
  // Too big to upload, or larger than 1080p (4K): re-encode once to 1080p. Other clips keep their
  // lossless passthrough copy. If resizing fails, the original goes up (the size check still applies).
  if (needsFit(out.size, video.longSide)) {
    try {
      const fitted = await SwingTrim.fitToSize(out.uri);
      // A 4K clip's 1080p copy can occasionally come out a little bigger; it is still the one to send.
      if (fitted.size > 0 && (fitted.size < out.size || fitted.size <= FIT_THRESHOLD_BYTES)) out = fitted;
    } catch {
      /* keep the passthrough copy */
    }
  }
  const name = out.uri.split('/').pop() ?? 'swing.mov';
  return { uri: out.uri, fileName: name, mimeType: out.mimeType, size: out.size, durationMs: out.durationMs };
}
