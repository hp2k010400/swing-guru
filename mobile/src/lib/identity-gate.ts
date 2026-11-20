// The one place that decides whether an in-flight operation still belongs to the signed-in account.
//
// Every awaited step (a token refresh, a body read, a deferred Apple sheet) can happen after the
// account changed. A request captures a snapshot before it starts and checks it again after each
// await; a mutation whose account changed is refused rather than applied to whoever is signed in now.

export interface IdentitySnapshot {
  owner: string | null;
  generation: number;
}

export interface IdentityGate {
  snapshot(): IdentitySnapshot;
  /** A different account (or none) invalidates every snapshot taken before it. */
  setOwner(next: string | null): void;
  /** Sign-out/deletion: invalidate even if the id itself did not change. */
  invalidate(): void;
  /** True while the snapshot still describes the signed-in account. */
  unchanged(taken: IdentitySnapshot): boolean;
}

export function createIdentityGate(): IdentityGate {
  let owner: string | null = null;
  let generation = 0;
  return {
    snapshot: () => ({ owner, generation }),
    setOwner(next: string | null) {
      if (next !== owner) {
        owner = next;
        generation += 1;
      }
    },
    invalidate() {
      owner = null;
      generation += 1;
    },
    unchanged(taken: IdentitySnapshot) {
      return taken.owner === owner && taken.generation === generation;
    },
  };
}

export function sameSnapshot(a: IdentitySnapshot, b: IdentitySnapshot): boolean {
  return a.owner === b.owner && a.generation === b.generation;
}

/**
 * Which identity an in-flight request must finish as.
 *
 * The first request of a cold start is taken before the anonymous bootstrap has resolved, so its
 * snapshot has no owner yet: adopting the owner the token resolution produced is the deliberate
 * first-run case, not a retarget. Once a request starts with a *known* owner, any change refuses it.
 */
export function resolveRequestIdentity(
  started: IdentitySnapshot,
  current: IdentitySnapshot,
): { ok: boolean; expected: IdentitySnapshot } {
  if (started.owner === null && current.owner !== null) {
    return { ok: true, expected: current };
  }
  return { ok: sameSnapshot(started, current), expected: started };
}
