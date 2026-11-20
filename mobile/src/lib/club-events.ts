// Lets the Summary update in place when its club is changed on the club screen.
import type { Club } from './types';

type Listener = (swingId: string, club: Club | null) => void;
const listeners = new Set<Listener>();

export function clubChanged(swingId: string, club: Club | null) {
  listeners.forEach((listener) => listener(swingId, club));
}

export function onClubChanged(listener: Listener) {
  listeners.add(listener);
  return () => {
    listeners.delete(listener);
  };
}
