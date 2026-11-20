// Lossless on-device trimming (see ios/SwingTrimModule.swift). Absent in Expo Go / web builds, where
// `available` is false and callers fall back to uploading the picked file as-is.
import { requireOptionalNativeModule } from 'expo';

export interface TrimmedVideo {
  uri: string;
  size: number;
  durationMs: number;
  mimeType: string;
}

export interface MotionProfile {
  samplesPerSecond: number;
  durationMs: number;
  values: number[];
}

interface SwingTrimNative {
  trim(uri: string, startMs: number, endMs: number): Promise<TrimmedVideo>;
  fitToSize?(uri: string): Promise<TrimmedVideo>;
  motionProfile(uri: string, samplesPerSecond: number): Promise<MotionProfile>;
}

const native = requireOptionalNativeModule<SwingTrimNative>('SwingTrim');

export const available = native !== null;

export function trim(uri: string, startMs: number, endMs: number): Promise<TrimmedVideo> {
  if (!native) return Promise.reject(new Error('Trimming needs the Swing Guru app build'));
  return native.trim(uri, startMs, endMs);
}

/** Re-encode a clip to 1080p (oversized or larger-than-1080p clips only; see needsFit in src/lib/trim.ts). */
export function fitToSize(uri: string): Promise<TrimmedVideo> {
  if (!native?.fitToSize) return Promise.reject(new Error('Resizing needs the Swing Guru app build'));
  return native.fitToSize(uri);
}

export function motionProfile(uri: string, samplesPerSecond: number): Promise<MotionProfile> {
  if (!native) return Promise.reject(new Error('Motion profile needs the Swing Guru app build'));
  return native.motionProfile(uri, samplesPerSecond);
}
