// The analytics client's logic, with no React Native import so tests can drive it with fakes.
// lib/analytics.ts binds it to the real storage, network, clock and app version.
//
// Rules: nothing is queued or sent without consent; failed sends are retried (bounded) instead of
// silently lost; "once" events (one completion per swing, one purchase per transaction) can never be
// sent twice, even across screen re-mounts or app restarts.

export type Value = string | number | boolean | null;
export type Consent = 'granted' | 'denied' | 'unset';

export interface KV {
  getItem(key: string): string | null;
  setItem(key: string, value: string): void;
  removeItem(key: string): void;
}

export interface CoreDeps {
  storage: () => KV | null;
  send: (body: string) => Promise<{ ok: boolean; status: number }>;
  now: () => number;
  newId: () => string;
  apiKey: string;
  base: () => Record<string, Value>;
  schedule: (fn: () => void, ms: number) => unknown;
  cancel: (handle: unknown) => void;
}

export const KEYS = {
  consent: 'sg.analytics.consent',
  id: 'sg.analytics.id',
  once: 'sg.analytics.once',
  firstOpen: 'sg.analytics.first_open_done',
} as const;

const MAX_QUEUE = 100;
const MAX_ONCE = 300;
const FLUSH_DELAY_MS = 3000;
/** A return to the foreground within this window is the same visit, not a new open. */
export const OPEN_GAP_MS = 30_000;

function clean(props: Record<string, Value>): Record<string, Value> {
  const out: Record<string, Value> = {};
  for (const [key, value] of Object.entries(props)) {
    if (typeof value === 'string') out[key] = value.slice(0, 40);
    else if (typeof value === 'number' || typeof value === 'boolean' || value === null) out[key] = value;
  }
  return out;
}

export function createAnalytics(deps: CoreDeps) {
  let queue: Record<string, unknown>[] = [];
  let timer: unknown = null;
  let sending = false;
  let lastOpen = 0;              // last cold start / foreground seen
  let lastCounted = -Infinity;   // last app_opened actually queued
  let firstThisVisit = false;    // whether the visit in progress is the install's first launch
  const listeners = new Set<() => void>();
  const available = deps.apiKey.startsWith('phc_');

  const store = () => {
    try {
      return deps.storage();
    } catch {
      return null;
    }
  };

  function consent(): Consent {
    const value = store()?.getItem(KEYS.consent);
    return value === 'granted' || value === 'denied' ? value : 'unset';
  }

  function analyticsId(): string {
    const s = store();
    let id = s?.getItem(KEYS.id);
    if (!id) {
      id = deps.newId();
      s?.setItem(KEYS.id, id);
    }
    return id;
  }

  function clearTimer() {
    if (timer !== null) deps.cancel(timer);
    timer = null;
  }

  function scheduleFlush(ms = FLUSH_DELAY_MS) {
    if (timer === null) timer = deps.schedule(() => { timer = null; void flush(); }, ms);
  }

  function track(event: string, props: Record<string, Value> = {}): boolean {
    if (!available || consent() !== 'granted') return false;
    queue.push({
      event,
      timestamp: new Date(deps.now()).toISOString(),
      properties: {
        ...deps.base(),
        ...clean(props),
        distinct_id: analyticsId(),
        $process_person_profile: false, // anonymous events: no person profiles
        $geoip_disable: true,           // no location lookup from the request
      },
    });
    if (queue.length > MAX_QUEUE) queue = queue.slice(-MAX_QUEUE);
    scheduleFlush();
    return true;
  }

  /** Send `event` at most once per `key` on this device (persisted). Only marks it once it is queued. */
  function trackOnce(event: string, key: string, props: Record<string, Value> = {}): boolean {
    const s = store();
    const tag = `${event}:${key}`;
    let seen: string[] = [];
    try {
      seen = JSON.parse(s?.getItem(KEYS.once) ?? '[]') as string[];
    } catch {
      seen = [];
    }
    if (seen.includes(tag)) return false;
    if (!track(event, props)) return false;
    seen.push(tag);
    s?.setItem(KEYS.once, JSON.stringify(seen.slice(-MAX_ONCE)));
    return true;
  }

  /** A cold start, or a return to the foreground after more than OPEN_GAP_MS. */
  function appOpened(source: 'cold_start' | 'foreground' | 'consent'): boolean {
    const now = deps.now();
    if (source === 'foreground' && now - lastOpen < OPEN_GAP_MS) return false;
    if (source === 'consent' && now - lastCounted < OPEN_GAP_MS) return false; // this visit already counted
    if (source !== 'consent') lastOpen = now;
    const s = store();
    // The first-launch marker is kept on the device whether or not analytics is allowed, so
    // first_open stays true only for a genuinely new install.
    const first = source === 'consent' ? firstThisVisit : !s?.getItem(KEYS.firstOpen);
    if (source !== 'consent') {
      firstThisVisit = first;
      if (first) s?.setItem(KEYS.firstOpen, '1');
    }
    const sent = track('app_opened', { source, first_open: first });
    if (sent) lastCounted = now;
    return sent;
  }

  async function flush(): Promise<void> {
    clearTimer();
    if (sending || !queue.length) return;
    if (consent() !== 'granted') { queue = []; return; }
    const batch = queue;
    queue = [];
    sending = true;
    try {
      const res = await deps.send(JSON.stringify({ api_key: deps.apiKey, batch }));
      // Server or rate-limit trouble: keep the events for a later try. A 4xx (bad payload or key)
      // would fail forever, so those are dropped rather than retried in a loop.
      if (!res.ok && (res.status >= 500 || res.status === 429)) requeue(batch);
    } catch {
      requeue(batch); // offline: try again with the next flush
    } finally {
      sending = false;
    }
  }

  function requeue(batch: Record<string, unknown>[]) {
    if (consent() !== 'granted') return;
    queue = [...batch, ...queue].slice(-MAX_QUEUE);
  }

  function setConsent(next: 'granted' | 'denied') {
    store()?.setItem(KEYS.consent, next);
    if (next === 'denied') {
      queue = []; // queued events are dropped, never sent
      clearTimer();
    }
    listeners.forEach((listener) => listener());
    // This visit's open was skipped while consent was unset; count it now that it is allowed.
    if (next === 'granted') appOpened('consent');
  }

  function resetId() {
    store()?.removeItem(KEYS.id);
    queue = [];
  }

  return {
    available,
    consent,
    setConsent,
    subscribe(listener: () => void) {
      listeners.add(listener);
      return () => { listeners.delete(listener); };
    },
    track,
    trackOnce,
    appOpened,
    flush,
    resetId,
    /** Tests only. */
    pending: () => queue.length,
  };
}
