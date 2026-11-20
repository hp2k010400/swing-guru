// Typed client for the analysis service (API v1). Errors become ApiError with golfer-facing text.
import { API_URL, STAGING_KEY } from '@/config';

import type { DeletionResult } from './account-core';
import { resolveRequestIdentity, sameSnapshot } from './identity-gate';
import { accessToken, refreshSession, sessionIdentity, userId, type SessionIdentity } from './session';
import { swingCache } from './swing-cache';
import type { BillingStatus, Club, Comparison, CreateJobResponse, Drill, Job, League, Progress, SwingDetail, SwingListItem } from './types';

export class ApiError extends Error {
  constructor(public status: number, public code: string, message: string) {
    super(message);
  }
}

const OFFLINE = new ApiError(0, 'offline', "You're offline. Check your connection and try again.");

function accountChanged(): ApiError {
  return new ApiError(401, 'account_changed', 'Your account changed. Please sign in again.');
}

async function request<T>(method: string, path: string, body?: unknown, retry = true): Promise<T> {
  // The account this request belongs to. Every await below can change it, so it is re-checked after
  // the token is resolved, after a refresh and before the answer is handed back - a mutation must
  // never be silently retargeted onto whoever is signed in later.
  const started: SessionIdentity = sessionIdentity();
  let expected: SessionIdentity = started;
  let response: Response;
  try {
    const token = await accessToken();
    // The first request of a cold start is taken before the anonymous bootstrap resolves; adopting
    // the resolved owner is the deliberate first-run case. A request that started with a known owner
    // refuses any change.
    const resolved = resolveRequestIdentity(started, sessionIdentity());
    if (!resolved.ok) throw accountChanged();
    expected = resolved.expected;
    response = await fetch(`${API_URL}${path}`, {
      method,
      headers: {
        Authorization: `Bearer ${token}`,
        ...(STAGING_KEY ? { 'X-SG-Access-Key': STAGING_KEY } : {}),
        ...(body ? { 'Content-Type': 'application/json' } : {}),
      },
      body: body ? JSON.stringify(body) : undefined,
    });
  } catch (error) {
    if (error instanceof ApiError) throw error;
    throw OFFLINE;
  }
  if (response.status === 401 && retry) {
    if (!sameSnapshot(sessionIdentity(), expected)) throw accountChanged();
    await refreshSession();
    if (!sameSnapshot(sessionIdentity(), expected)) throw accountChanged();
    return request<T>(method, path, body, false);
  }
  if (response.status === 204) {
    if (!sameSnapshot(sessionIdentity(), expected)) throw accountChanged();
    return undefined as T;
  }
  let json: any = null;
  try {
    json = await response.json();
  } catch {
    /* not JSON */
  }
  if (!response.ok) {
    const error = json?.error ?? {};
    const message =
      response.status >= 500
        ? 'Something went wrong on our side. Please try again in a moment.'
        : error.message ?? 'Something went wrong. Please try again.';
    throw new ApiError(response.status, error.code ?? 'error', message);
  }
  if (!sameSnapshot(sessionIdentity(), expected)) throw accountChanged();
  return json as T;
}

export const api = {
  createJob: (body: { file_name: string; content_type: string; size_bytes: number; captured_at?: string | null; club?: Club | null }) =>
    request<CreateJobResponse>('POST', '/v1/analysis/jobs', body),
  startJob: (id: string) => request<Job>('POST', `/v1/analysis/jobs/${id}/start`),
  job: (id: string) => request<Job>('GET', `/v1/analysis/jobs/${id}`),
  swings: () => request<{ swings: SwingListItem[] }>('GET', '/v1/swings').then((r) => r.swings),
  swing: async (id: string) => {
    // The ticket is taken for the account signed in *now*: if it changes while this request is in
    // flight, the response is not cached (and the caller still gets its own answer).
    swingCache.setOwner(await userId());
    const ticket = swingCache.ticket();
    const detail = await request<SwingDetail>('GET', `/v1/swings/${id}`);
    swingCache.put(ticket, id, detail);
    return detail;
  },
  // The shared, read-only example analysis (new users). 404 when the server has none: callers hide it.
  example: () => request<SwingDetail>('GET', '/v1/example'),
  setClub: (id: string, club: Club | null) => request<SwingListItem>('PATCH', `/v1/swings/${id}`, { club }),
  deleteSwing: (id: string) => request<void>('DELETE', `/v1/swings/${id}`),
  // The Apple authorization code is only needed for an account linked before the server stored a
  // refresh token; `manual_apple_revocation` is the explicit TN3194 fallback when no credential can
  // be obtained. The answer says which of those really happened.
  deleteAccount: (body?: { authorization_code?: string; nonce?: string; manual_apple_revocation?: boolean }) =>
    request<DeletionResult>('DELETE', '/v1/account', body),
  // One-shot registration of the Apple grant (authorization code -> server-side refresh token).
  registerApple: (body: { authorization_code: string; nonce: string }) =>
    request<void>('POST', '/v1/account/apple', body),
  drills: () => request<{ groups: { id: string; label: string; drills: Drill[] }[] }>('GET', '/v1/drills'),
  progress: () => request<Progress>('GET', '/v1/progress'),
  // Weekly leagues (staging servers only for now: a server without them answers 404, and the app hides them).
  league: () => request<League>('GET', '/v1/league'),
  leagueNames: (page = 0) => request<{ names: string[] }>('GET', `/v1/league/names?page=${page}`).then((r) => r.names),
  leagueJoin: (display_name: string) => request<League>('POST', '/v1/league/join', { display_name }),
  leagueRename: (display_name: string) => request<League>('PATCH', '/v1/league/profile', { display_name }),
  leagueLeave: () => request<void>('DELETE', '/v1/league/profile'),
  compare: (a: string, b: string) => request<Comparison>('GET', `/v1/compare?a=${encodeURIComponent(a)}&b=${encodeURIComponent(b)}`),
  // Subscription state is always the server's answer for the verified user; the client never sends
  // an entitlement, product or customer id.
  billing: () => request<BillingStatus>('GET', '/v1/billing'),
  billingSync: () => request<BillingStatus>('POST', '/v1/billing/sync', {}),
  // In-app feedback; the server adds the swing's stored score context.
  feedback: (body: Record<string, unknown>) => request<{ status: string }>('POST', '/v1/feedback', body),
};

// The last Summary fetched per swing, for the signed-in account only. Replay reuses its signed URLs
// (valid for an hour) instead of another round trip, so the viewer can start loading at once.
/** Route id for the example analysis: /swing/example and /replay/example. */
export const EXAMPLE_ID = 'example';

export function cachedSwing(id: string): SwingDetail | null {
  return swingCache.get(id);
}

export function friendly(error: unknown): string {
  return error instanceof ApiError ? error.message : 'Something went wrong. Please try again.';
}
