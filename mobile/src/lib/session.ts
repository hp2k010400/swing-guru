// Identity: a Supabase Auth session. On first launch the app signs in *anonymously* - a real
// auth.users id that owns every swing. Linking Sign in with Apple later keeps the same id,
// so no data migration is needed. "dev" mode (local development service only) uses a
// random device id instead.
import 'expo-sqlite/localStorage/install';

import { createClient, type SupabaseClient } from '@supabase/supabase-js';
import * as Crypto from 'expo-crypto';
import { AppState } from 'react-native';

import { AUTH_MODE, SUPABASE_ANON_KEY, SUPABASE_URL } from '@/config';
import { createIdentityGate, sameSnapshot, type IdentitySnapshot } from './identity-gate';
import { createSessionBootstrap } from './session-bootstrap';
import { swingCache } from './swing-cache';

export const supabase: SupabaseClient | null =
  AUTH_MODE === 'supabase'
    ? createClient(SUPABASE_URL, SUPABASE_ANON_KEY, {
        auth: { storage: localStorage, autoRefreshToken: true, persistSession: true, detectSessionInUrl: false },
      })
    : null;

/**
 * Bumped whenever the signed-in *account* changes (a different user id, a sign-out or a deletion).
 * An in-flight API call captures it (with the owner) and refuses to finish or retry across the
 * change, so a request can never carry one golfer's action into another golfer's session.
 */
const identityGate = createIdentityGate();

export type SessionIdentity = IdentitySnapshot;

/** The account a request must still belong to when it finishes. */
export function sessionIdentity(): SessionIdentity {
  return identityGate.snapshot();
}

export function sameIdentity(a: SessionIdentity, b: SessionIdentity): boolean {
  return sameSnapshot(a, b);
}

/**
 * One auth transition at a time. link/sign-in/switch/sign-out change which account every other
 * in-flight operation belongs to, so letting two of them interleave is how a guest link turns into
 * a silent switch (or a sign-out of the wrong account).
 */
let transitionQueue: Promise<unknown> = Promise.resolve();

export function runAuthTransition<T>(operation: () => Promise<T>): Promise<T> {
  const run = transitionQueue.then(operation, operation);
  transitionQueue = run.then(() => undefined, () => undefined);
  return run;
}

if (supabase) {
  // Cached Summaries (and their signed media URLs) belong to one account: any change of user clears them.
  supabase.auth.onAuthStateChange((_event, session) => {
    const owner = session?.user?.id ?? null;
    identityGate.setOwner(owner);
    swingCache.setOwner(owner);
  });
  AppState.addEventListener('change', (state) => {
    if (state === 'active') supabase.auth.startAutoRefresh();
    else supabase.auth.stopAutoRefresh();
  });
}

const DEV_KEY = 'sg.devUserId';

/**
 * One session at a time: concurrent callers share a single anonymous sign-in, so two screens
 * racing on a cold start cannot create two Supabase users.
 */
const bootstrap = createSessionBootstrap<{ access_token: string; user: { id: string } }>({
  readSession: async () => {
    if (!supabase) return null;
    const { data } = await supabase.auth.getSession();
    return data.session ?? null;
  },
  createSession: async () => {
    if (!supabase) throw new Error('supabase is not configured');
    const { data: signedIn, error } = await supabase.auth.signInAnonymously();
    if (error || !signedIn.session) throw new Error('sign-in failed');
    return signedIn.session;
  },
});

/**
 * The public bootstrap, serialised with every auth transition. Bootstrapping can *create* an
 * anonymous account, so letting it run beside a link or a sign-out is how one golfer's device ends
 * up with an account nobody chose.
 */
export function ensureSession() {
  return runAuthTransition(bootstrap);
}

/**
 * Internal only: for code already running inside a transition (the queue is held there, so calling
 * the serialised form would deadlock). Never use this from ordinary screens or requests.
 */
export function ensureSessionUnlocked() {
  return bootstrap();
}

function devToken(): string {
  let id = localStorage.getItem(DEV_KEY);
  if (!id) {
    id = Crypto.randomUUID();
    localStorage.setItem(DEV_KEY, id);
  }
  return `dev:${id}`;
}

/** Test builds only (dev sign-in): this device's test account id, shown in Profile so test data can be
 *  attached to it on the staging server. Null with a real Supabase session. */
export function devUserId(): string | null {
  if (supabase) return null;
  return devToken().slice('dev:'.length);
}

export async function accessToken(): Promise<string> {
  if (!supabase) return devToken();
  return (await ensureSession())!.access_token;
}

/** Internal only (see ``ensureSessionUnlocked``). */
export async function accessTokenUnlocked(): Promise<string> {
  if (!supabase) return devToken();
  return (await ensureSessionUnlocked())!.access_token;
}

/**
 * The RevenueCat App User ID: the same Supabase UUID (never an invented identity). Resolved at
 * purchase time so the identity cannot be a stale or unauthenticated one.
 */
export async function userId(): Promise<string> {
  if (!supabase) return (await accessToken()).slice(4);
  return (await ensureSession())!.user.id;
}

export async function refreshSession(): Promise<void> {
  return runAuthTransition(async () => {
    if (supabase) await supabase.auth.refreshSession();
  });
}

/** Internal only (see ``ensureSessionUnlocked``). */
export async function refreshSessionUnlocked(): Promise<void> {
  if (supabase) await supabase.auth.refreshSession();
}

export async function signOutLocally(): Promise<void> {
  identityGate.invalidate();
  swingCache.clear();
  if (!supabase) {
    localStorage.removeItem(DEV_KEY);
    return;
  }
  try {
    // Local only: the server-side account may already be gone, and the local caches above are the
    // part that must be gone either way. A storage/SDK failure must not leave the caller thinking
    // the session is still usable.
    await supabase.auth.signOut({ scope: 'local' });
  } catch {
    /* the local caches and the owner marker are already cleared */
  }
}
