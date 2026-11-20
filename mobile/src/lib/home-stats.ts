// Home statistics, all derived from real analysed swings. No scores: counts of measured checks only.
import type { SymbolViewProps } from 'expo-symbols';

import type { Status } from '@/theme';

import type { SwingDetail, SwingListItem } from './types';

function when(s: Pick<SwingListItem, 'captured_at' | 'created_at'>): Date {
  return new Date(s.captured_at ?? s.created_at);
}

/** Monday 00:00 (local) of the week containing `date`, `weeksBack` weeks earlier. Stepped in calendar
 *  days: a fixed 7 x 24 h step lands on Sunday 23:00 across a clock change and broke streaks. */
function weekStart(date: Date, weeksBack = 0): number {
  return new Date(date.getFullYear(), date.getMonth(), date.getDate() - ((date.getDay() + 6) % 7) - 7 * weeksBack).getTime();
}

/** Consecutive weeks with at least one swing, ending this week (or last week if none yet this week). */
export function weekStreak(swings: SwingListItem[], now = new Date()): number {
  const weeks = new Set(swings.map((s) => weekStart(when(s))));
  let back = weeks.has(weekStart(now)) ? 0 : 1;
  let streak = 0;
  while (weeks.has(weekStart(now, back))) {
    streak += 1;
    back += 1;
  }
  return streak;
}

export interface Recurring {
  id: string;
  name: string;
  phaseLabel: string;
  outOf: number; // swings in which it was measured
  out: number; // ...and out of range
  /** Oldest -> newest, one entry per recent swing: its status for this check (null = not measured). */
  history: (Status | null)[];
  latestSwingId: string;
}

const OUT: Status[] = ['attention', 'problem'];

/** The check most often out of range across recent swings (ties: the most recent miss wins). */
export function mostOftenOut(details: SwingDetail[]): Recurring | null {
  const recent = [...details].reverse(); // oldest -> newest
  const tally = new Map<string, Recurring>();
  recent.forEach((swing, index) => {
    for (const c of swing.checkpoints) {
      const entry = tally.get(c.id) ?? {
        id: c.id, name: c.name, phaseLabel: c.phase_label, outOf: 0, out: 0,
        history: recent.map(() => null), latestSwingId: swing.id,
      };
      if (c.status !== 'info' && c.status !== 'withheld') {
        entry.outOf += 1;
        entry.history[index] = c.status;
        if (OUT.includes(c.status)) {
          entry.out += 1;
          entry.latestSwingId = swing.id;
        }
      }
      tally.set(c.id, entry);
    }
  });
  const ranked = [...tally.values()].filter((e) => e.out > 0)
    .sort((a, b) => b.out - a.out || b.out / b.outOf - a.out / a.outOf);
  return ranked[0] ?? null;
}

export interface Milestone {
  id: string;
  title: string;
  icon: SymbolViewProps['name'];
  earned: boolean;
}

export function milestones(swings: SwingListItem[], details: SwingDetail[]): Milestone[] {
  const n = swings.length;
  const streak = weekStreak(swings);
  const clubs = new Set(swings.map((s) => s.club).filter(Boolean)).size;
  const setupClean = details.some((d) => {
    const setup = d.checkpoints.filter((c) => c.phase === 'address' && c.status !== 'withheld' && c.status !== 'info');
    return setup.length > 0 && setup.every((c) => c.status === 'good');
  });
  const allClean = swings.some((s) => s.counts && s.counts.total > 0 && s.counts.good === s.counts.total);
  return [
    { id: 'first', title: 'First swing', icon: 'figure.golf', earned: n >= 1 },
    { id: 'five', title: '5 swings', icon: '5.circle.fill', earned: n >= 5 },
    { id: 'ten', title: '10 swings', icon: '10.circle.fill', earned: n >= 10 },
    { id: 'streak2', title: '2-week streak', icon: 'flame.fill', earned: streak >= 2 },
    { id: 'setup', title: 'Solid set-up', icon: 'checkmark.seal.fill', earned: setupClean },
    { id: 'clubs', title: '3 clubs', icon: 'bag.fill', earned: clubs >= 3 },
    { id: 'clean', title: 'All in range', icon: 'star.fill', earned: allClean },
  ];
}
