// Cache of Swing Summary details (with their signed media URLs), bound to the signed-in account.
//
// Kept dependency-free so it can be tested directly. Security rules:
// - every entry belongs to the owner (Supabase user id) that was signed in when it was fetched;
// - changing owner, signing out or deleting the account clears everything and bumps a generation;
// - a fetch remembers the generation it started in, and its result is refused if the owner or the
//   generation changed while it was in flight (an old account's response can never refill the cache);
// - signed URLs are only served while they still have `marginS` seconds of validity left.

export interface Expiring {
  replay: { expires_in_s: number };
}

export interface FetchTicket {
  generation: number;
  owner: string | null;
}

export function createDetailCache<T extends Expiring>(now: () => number = () => Date.now(), marginS = 600) {
  let generation = 0;
  let owner: string | null = null;
  const entries = new Map<string, { detail: T; at: number }>();

  const reset = () => {
    generation += 1;
    entries.clear();
  };

  return {
    /** The account now signed in (null when signed out). A different owner clears the cache. */
    setOwner(next: string | null) {
      if (next !== owner) {
        owner = next;
        reset();
      }
    },
    /** Sign-out / account deletion: forget everything, even for the same owner id. */
    clear() {
      owner = null;
      reset();
    },
    /** Taken *before* a request starts; required to store its result. */
    ticket(): FetchTicket {
      return { generation, owner };
    },
    /** Stores the result only if the same owner is still signed in and nothing was cleared since. */
    put(ticket: FetchTicket, id: string, detail: T): boolean {
      if (owner === null || ticket.owner !== owner || ticket.generation !== generation) return false;
      entries.set(id, { detail, at: now() });
      return true;
    },
    get(id: string): T | null {
      const hit = entries.get(id);
      if (!hit) return null;
      const ageS = (now() - hit.at) / 1000;
      if (ageS >= hit.detail.replay.expires_in_s - marginS) {
        entries.delete(id);
        return null;
      }
      return hit.detail;
    },
    size: () => entries.size,
  };
}

export type DetailCache<T extends Expiring> = ReturnType<typeof createDetailCache<T>>;
