// Week streaks across the October clock change (a fixed 7 x 24 h step used to break them).
import assert from 'node:assert/strict';
import { test } from 'node:test';

import { weekStreak } from '../src/lib/home-stats.ts';
import type { SwingListItem } from '../src/lib/types.ts';

const at = (y: number, m: number, d: number): SwingListItem =>
  ({ id: `${m}-${d}`, captured_at: null, created_at: new Date(y, m, d, 10).toISOString() } as SwingListItem);

test('three consecutive weeks spanning a clock change count as three', () => {
  process.env.TZ = process.env.TZ ?? 'Europe/London';
  const now = new Date(2026, 10, 6, 12);
  assert.equal(weekStreak([at(2026, 10, 6), at(2026, 9, 30), at(2026, 9, 23)], now), 3);
  assert.equal(weekStreak([at(2026, 9, 30), at(2026, 9, 23)], now), 2);   // none this week yet: counts back from last week
  assert.equal(weekStreak([at(2026, 9, 23)], now), 0);                    // a gap ends the streak
});
