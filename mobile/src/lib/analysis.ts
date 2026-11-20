// Getting a swing analysed: pick/record -> trim -> club -> create job -> upload -> start -> the server
// works. Only the trimmed selection is uploaded. The upload runs as an iOS background session, and the
// job id is persisted, so leaving the screen, backgrounding or even restarting the app never loses an
// analysis.
import { File, UploadType } from 'expo-file-system';
import * as ImagePicker from 'expo-image-picker';

import { clubGroup, track } from './analytics';
import { api, ApiError } from './api';
import { classifyStartFailure, nextPhaseForRetry } from './job-start';
import { reconcileAction } from './pending-reconcile';
import type { Club, JobStatus } from './types';

export type UploadPhase = 'uploading' | 'uploaded' | 'upload_failed';
// The bytes uploaded fine but the server needs a plan (402) or a durable account (403) before the
// analysis can start; or the server analysed it and could not find a swing ('analysis_failed').
export type PendingPhase = UploadPhase | 'purchase_required' | 'sign_in_required' | 'analysis_failed';

// Uploads running in this app session. While one runs, nothing else may ask the server to /start
// that job: the bytes are still arriving (Build 16 started jobs mid-upload; staging then analysed a
// truncated video and reported "no swing").
const running = new Set<string>();

export function uploadRunning(id: string): boolean {
  return running.has(id);
}

interface Pending {
  id: string;
  created: number;
  phase: PendingPhase;
  fileUri: string;
  error?: string;
}

const KEY = 'sg.pendingJobs';
const listeners = new Set<() => void>();

function read(): Pending[] {
  try {
    return JSON.parse(localStorage.getItem(KEY) ?? '[]') as Pending[];
  } catch {
    return [];
  }
}

function write(items: Pending[]): void {
  localStorage.setItem(KEY, JSON.stringify(items));
  listeners.forEach((l) => l());
}

export const pending = {
  all: read,
  get: (id: string) => read().find((p) => p.id === id) ?? null,
  set(item: Pending) {
    write([...read().filter((p) => p.id !== item.id), item]);
  },
  remove(id: string) {
    write(read().filter((p) => p.id !== id));
  },
  subscribe(listener: () => void) {
    listeners.add(listener);
    return () => listeners.delete(listener);
  },
};

export interface PickedVideo {
  uri: string;
  fileName: string;
  mimeType: string;
  size: number;
  durationMs: number | null;
  /** Longest side of the picked original in pixels, when the picker reports it (4K = 3840). */
  longSide?: number;
}

const MAX_BYTES = 500 * 1024 * 1024;
const MAX_SECONDS = 180;
/** Largest single upload storage accepts (Supabase project limit). The server enforces the same. */
export const UPLOAD_MAX_BYTES = 50 * 1024 * 1024;

function toPicked(asset: ImagePicker.ImagePickerAsset): PickedVideo {
  let size = asset.fileSize ?? 0;
  if (!size) {
    try {
      size = new File(asset.uri).size ?? 0;
    } catch {
      size = 0;                                        // unknown here; the trimmed file's own size is what's uploaded
    }
  }
  const name = asset.fileName ?? `swing${asset.uri.toLowerCase().endsWith('.mov') ? '.mov' : '.mp4'}`;
  const mimeType = asset.mimeType ?? (name.toLowerCase().endsWith('.mov') ? 'video/quicktime' : 'video/mp4');
  const longSide = Math.max(asset.width ?? 0, asset.height ?? 0);
  return { uri: asset.uri, fileName: name, mimeType, size, durationMs: asset.duration ?? null, ...(longSide > 0 ? { longSide } : {}) };
}

/** Record with the system camera, or choose from Photos. The original file is kept (Passthrough export). */
export async function pickVideo(source: 'camera' | 'library'): Promise<PickedVideo | null> {
  const permission =
    source === 'camera' ? await ImagePicker.requestCameraPermissionsAsync() : await ImagePicker.requestMediaLibraryPermissionsAsync();
  if (!permission.granted) throw new ApiError(0, 'permission', source === 'camera'
    ? 'Swing Guru needs camera access to record your swing. You can allow it in Settings.'
    : 'Swing Guru needs access to your videos to analyse a swing. You can allow it in Settings.');
  const options: ImagePicker.ImagePickerOptions = {
    mediaTypes: ['videos'],
    allowsEditing: false,
    videoMaxDuration: 60,
    videoQuality: ImagePicker.UIImagePickerControllerQualityType.High,
    videoExportPreset: ImagePicker.VideoExportPreset.Passthrough,
    preferredAssetRepresentationMode: ImagePicker.UIImagePickerPreferredAssetRepresentationMode.Current,
  };
  const result = source === 'camera' ? await ImagePicker.launchCameraAsync(options) : await ImagePicker.launchImageLibraryAsync(options);
  if (result.canceled || !result.assets?.length) return null;
  return toPicked(result.assets[0]);
}

/** The picked original (it is trimmed on the phone, so only its format and length matter here). */
export function checkVideo(video: PickedVideo): string | null {
  if (!['video/mp4', 'video/quicktime', 'video/x-m4v'].includes(video.mimeType)) return 'Please choose an MP4 or MOV video.';
  if (video.size > MAX_BYTES) return 'This video is too large. Trim it to one swing, or record at 1080p.';
  if (video.durationMs !== null && video.durationMs / 1000 > MAX_SECONDS) return 'This video is too long. Trim it to one swing and try again.';
  if (video.durationMs !== null && video.durationMs < 1500) return 'This video is too short. Record from set-up to finish.';
  return null;
}

/** The trimmed selection that will be uploaded. Passthrough trimming can start up to a keyframe early. */
export function checkTrimmed(video: PickedVideo): string | null {
  if (video.size > UPLOAD_MAX_BYTES) return 'This selection is too large to upload. Choose a shorter part of the clip, or record at 1080p.';
  if (video.durationMs !== null && video.durationMs < 1500) return 'This selection is too short. Include your set-up and finish.';
  if (video.durationMs !== null && video.durationMs > 15_000) return 'This selection is too long. Choose just one swing.';
  return null;
}

/** Create the job and start uploading. Returns the job id immediately; the upload continues in the background. */
export async function submit(video: PickedVideo, club: Club | null = null): Promise<string> {
  const created = await api.createJob({
    file_name: video.fileName,
    content_type: video.mimeType,
    size_bytes: video.size,
    club,
  });
  const id = created.job.id;
  track('swing_analysis_started', { club_group: clubGroup(club) });
  pending.set({ id, created: Date.now(), phase: 'uploading', fileUri: video.uri });
  void upload(id, video.uri, created.upload);
  return id;
}

async function upload(id: string, fileUri: string, instructions: { url: string; headers: Record<string, string> }) {
  running.add(id);
  try {
    await uploadAndStart(id, fileUri, instructions);
  } finally {
    running.delete(id);
  }
}

async function uploadAndStart(id: string, fileUri: string, instructions: { url: string; headers: Record<string, string> }) {
  try {
    const task = new File(fileUri).createUploadTask(instructions.url, {
      httpMethod: 'PUT',
      headers: instructions.headers,
      uploadType: UploadType.BINARY_CONTENT,
      sessionType: 'background', // iOS keeps uploading if the app is backgrounded
    });
    const result = await task.uploadAsync();
    if (!result || result.status >= 300) throw new Error(`upload ${result?.status}`);
    pending.set({ ...(pending.get(id) as Pending), phase: 'uploaded' });
    try {
      await startWhenStored(id);
      // Queued: the trimmed copy on the phone is no longer needed.
      discardTrim(fileUri);
    } catch (error) {
      // The bytes are on the server; only an expired/missing upload needs the video again.
      const failure = classifyStartFailure(toStartError(error), Date.now() - (pending.get(id)?.created ?? Date.now()));
      const current = pending.get(id) as Pending;
      pending.set({ ...current, phase: failure.phase, ...(failure.message ? { error: failure.message } : {}) });
      if (failure.chooseAgain) discardTrim(fileUri);
    }
  } catch {
    const current = pending.get(id);
    if (!current) return;
    pending.set({ ...current, phase: 'upload_failed', error: "Your video didn't finish uploading." });
  }
}

/** /start right after the upload finished: the storage may need a moment to show the whole file. */
async function startWhenStored(id: string): Promise<void> {
  for (let attempt = 0; ; attempt += 1) {
    try {
      await api.startJob(id);
      return;
    } catch (error) {
      if (!(error instanceof ApiError && error.code === 'upload_missing') || attempt >= 3) throw error;
      await new Promise((resolve) => setTimeout(resolve, 1500 * (attempt + 1)));
    }
  }
}

/**
 * Bring the phone's remembered uploads in line with the server (Home on focus, app start). Clears
 * entries for swings that finished, shows server-side analysis failures as such, and starts jobs whose
 * bytes arrived in the background - so a stale "Upload stopped" never sits above an analysed swing.
 */
export async function reconcilePending(): Promise<void> {
  for (const item of read()) {
    let server: { status: JobStatus } | 'missing';
    try {
      server = await api.job(item.id);
    } catch (error) {
      if (error instanceof ApiError && error.status === 404) server = 'missing';
      else continue;                                   // offline: leave it as it is
    }
    const action = reconcileAction(item, server, running.has(item.id));
    if (action.kind === 'remove') pending.remove(item.id);
    else if (action.kind === 'set') pending.set({ ...item, phase: action.phase, error: undefined });
    else if (action.kind === 'start') await resume(item.id, { reconcile: true });
  }
}

function toStartError(error: unknown): { status?: number; code?: string } {
  return error instanceof ApiError ? { status: error.status, code: error.code } : { status: 0 };
}

/** Trimmed copies live in the app's cache; remove one once the server has it. Originals are never touched. */
function discardTrim(fileUri: string) {
  if (!fileUri.includes('/swing-trims/')) return;
  try {
    new File(fileUri).delete();
  } catch {
    /* the OS clears the cache eventually */
  }
}

/** After an app restart: an upload that finished in the background still needs its job started. */
export async function resume(id: string, options: { reconcile?: boolean } = {}): Promise<void> {
  const item = pending.get(id);
  if (!item || item.phase === 'analysis_failed') return;
  // A stopped upload is only re-tried when the server says the job is still waiting for its bytes
  // (reconcilePending), never blindly; and a running upload is never interrupted by a /start.
  if (item.phase === 'upload_failed' && !options.reconcile) return;
  if (running.has(id)) return;
  try {
    await api.startJob(id);
    pending.set({ ...item, phase: 'uploaded' });
    discardTrim(item.fileUri);
  } catch (error) {
    const failure = classifyStartFailure(toStartError(error), Date.now() - item.created);
    const phase = nextPhaseForRetry(item.phase, failure);
    if (phase !== item.phase || failure.message) {
      // 'uploaded' (offline / billing_unavailable / plan now active) keeps retrying this same job;
      // 'purchase_required' and 'sign_in_required' keep the job and the draft; 'upload_failed' means
      // the bytes are gone.
      pending.set({ ...item, phase, ...(failure.message ? { error: failure.message } : {}) });
    }
    if (failure.chooseAgain) discardTrim(item.fileUri);
  }
}
