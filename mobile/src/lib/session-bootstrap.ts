// Session initialization helpers, kept dependency-free so they can be tested directly.
//
// Two callers can ask for the session at the same moment (the root layout warming the session and
// a screen starting an API call). Without single-flight they would both see "no session" and both
// call signInAnonymously, creating two Supabase users; the second would silently take over.

/** Run `operation` at most once at a time; concurrent callers share the result. */
export function singleFlight<T>(operation: () => Promise<T>): () => Promise<T> {
  let inFlight: Promise<T> | null = null;
  return () => {
    if (!inFlight) {
      // Clear the slot from *inside* the tracked promise, so an awaiter that resumes and calls
      // again immediately re-runs the operation instead of receiving the stale settled value
      // (for example after a sign-out between two awaits).
      const tracked: Promise<T> = operation().then(
        (value) => {
          if (inFlight === tracked) inFlight = null;
          return value;
        },
        (error) => {
          if (inFlight === tracked) inFlight = null;
          throw error;
        },
      );
      inFlight = tracked;
    }
    return inFlight;
  };
}

export interface SessionBootstrapDeps<T> {
  /** The current session, or null when there is none. */
  readSession(): Promise<T | null>;
  /** Create a new session (anonymous sign-in). */
  createSession(): Promise<T>;
}

/**
 * Resolve a session: reuse the existing one, otherwise create exactly one even under concurrency.
 *
 * The *whole* read-or-create is single-flighted, not just the creation. Otherwise a slow
 * `readSession` that resolves to null after another caller has already created (and the creation
 * has settled) would start a second anonymous sign-in. A failed resolution is not cached, so the
 * next call retries.
 */
export function createSessionBootstrap<T>(deps: SessionBootstrapDeps<T>): () => Promise<T | null> {
  return singleFlight(async () => (await deps.readSession()) ?? deps.createSession());
}
