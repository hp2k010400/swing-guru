// Processing-screen feedback decisions, kept dependency-free so they can be tested directly.
//
// The server reports five real stages (DIAGNOSTICS.md); the client never invents progress. This
// tracker turns successive job snapshots into at most one feedback cue per observation:
// - the first snapshot of a job only *hydrates* (stages already done never "complete" again);
// - several stages finishing between two polls give one cue, not a burst;
// - after returning from the background the next snapshot is absorbed silently (no catch-up);
// - Ready is announced once per job, and only when the job itself is complete (not when the
//   engine's last artifact appears);
// - a different job id starts fresh (hydrates again).

export type StageState = 'pending' | 'active' | 'done';
export type JobStatus = 'awaiting_upload' | 'queued' | 'processing' | 'complete' | 'failed';
export type Cue = 'stage' | 'ready' | null;

export interface Snapshot {
  jobId: string;
  status: JobStatus;
  uploadDone: boolean;
  stages: { id: string; state: StageState }[];
}

function doneIds(snapshot: Snapshot): Set<string> {
  const ids = new Set(snapshot.stages.filter((s) => s.state === 'done').map((s) => s.id));
  if (snapshot.uploadDone) ids.add('upload');
  return ids;
}

export function createProgressTracker() {
  let jobId: string | null = null;
  let seen = new Set<string>();
  let ready = false;
  let quiet = false;

  return {
    /** The app came back to the foreground: absorb the next snapshot without feedback. */
    quietNext() {
      quiet = true;
    },
    observe(snapshot: Snapshot, foreground = true): Cue {
      const done = doneIds(snapshot);
      if (snapshot.jobId !== jobId) {
        jobId = snapshot.jobId;
        seen = done;
        ready = snapshot.status === 'complete';
        quiet = false;
        return null; // hydration: nothing is announced
      }
      const fresh = [...done].filter((id) => !seen.has(id));
      fresh.forEach((id) => seen.add(id));
      const silent = quiet || !foreground;
      quiet = false;
      if (snapshot.status === 'complete' && !ready) {
        ready = true;
        return foreground ? 'ready' : null; // Ready is never swallowed by a quiet foreground return
      }
      if (fresh.length && !silent) return 'stage';
      return null;
    },
  };
}

// ---------------------------------------------------------------- haptics gate

export type HapticKind = 'selection' | 'stage' | 'success' | 'reveal';

/**
 * Rate limits for haptics: stage cues at most every `stageGapMs`, one success per key (job), and a
 * score-reveal cue is skipped when a success cue just fired (the Ready buzz already covered it).
 */
export function createHapticGate(now: () => number = () => Date.now(), stageGapMs = 1200, revealAfterSuccessMs = 4000) {
  let lastStage = -Infinity;
  let lastSuccess = -Infinity;
  const succeeded = new Set<string>();
  const revealed = new Set<string>();
  return {
    allow(kind: HapticKind, key = ''): boolean {
      const t = now();
      if (kind === 'selection') return true;
      if (kind === 'stage') {
        if (t - lastStage < stageGapMs) return false;
        lastStage = t;
        return true;
      }
      if (kind === 'success') {
        if (succeeded.has(key)) return false;
        succeeded.add(key);
        lastSuccess = t;
        return true;
      }
      // reveal
      if (revealed.has(key) || t - lastSuccess < revealAfterSuccessMs) {
        revealed.add(key);
        return false;
      }
      revealed.add(key);
      return true;
    },
  };
}

// ---------------------------------------------------------------- replay state

export interface ReplayState {
  run: number;
  ready: boolean;
  ended: boolean;
}

export type ReplayEvent =
  | { type: 'message'; run: number; kind: string }
  | { type: 'watchAgain' };

/** Viewer messages from an old run (before Watch Again) are ignored; the stored score is never re-derived. */
export function replayReducer(state: ReplayState, event: ReplayEvent): ReplayState {
  if (event.type === 'watchAgain') return { run: state.run + 1, ready: false, ended: false };
  if (event.run !== state.run) return state;
  if (event.kind === 'ready') return state.ready ? state : { ...state, ready: true };
  if (event.kind === 'ended') return state.ended ? state : { ...state, ready: true, ended: true };
  return state;
}

/** The score shown at the start of a reveal: the final value at once when motion is reduced or not fresh. */
export function revealFrom(total: number, { fresh, reducedMotion }: { fresh: boolean; reducedMotion: boolean }): number {
  return fresh && !reducedMotion ? 0 : total;
}
