// In-app navigation memory: where the user came from, and each screen's scroll position.

const trail = [];          // visited hashes, newest last
const scrolls = new Map(); // hash -> scrollTop

export function visited(hash) {
  if (trail[trail.length - 1] !== hash) trail.push(hash);
  if (trail.length > 40) trail.shift();
}

export const previous = () => trail[trail.length - 2] || null;

/** Go back to the previous screen if it is `expected` (or anything, when omitted); else to `fallback`. */
export function back(fallback, expected = null) {
  const prev = previous();
  if (prev && (!expected || prev === expected)) {
    trail.pop();
    history.back();
  } else {
    location.replace(fallback);
  }
}

/** Replace the current entry (processing -> summary should not be revisited with Back). */
export function replace(hash) {
  trail.pop();
  location.replace(hash);
}

export const rememberScroll = (hash, top) => scrolls.set(hash, top);
export const scrollFor = (hash) => scrolls.get(hash) || 0;
