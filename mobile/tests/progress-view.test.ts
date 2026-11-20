import assert from 'node:assert/strict';
import { test } from 'node:test';

import { achievements, consistency, nextObjective, notCounted, practice, shouldCelebrate, trend } from '../src/lib/progress-view.ts';
import type { Progress, ProgressSwing, RankInfo } from '../src/lib/types.ts';

const swing = (i: number, score: number | null, status: 'good' | 'attention' = 'good', day = i): ProgressSwing => ({
  id: String(i), created_at: new Date(2026, 9, 1 + day, 10).toISOString(), club: '7 Iron', score,
  checks: [{ id: 'top.posture', status, value: 0, unit: 'deg' }],
});

test('trend compares the median of the last five with the five before', () => {
  const list = [6, 6, 6, 6, 6, 8, 8, 8, 8, 8].map((s, i) => swing(i, s));
  assert.deepEqual(trend(list), { recent: 8, before: 6, delta: 2 });
  assert.equal(trend(list.slice(0, 9)), null);
});

test('consistency counts in-range readings over the last ten measured', () => {
  const list = Array.from({ length: 12 }, (_, i) => swing(i, 7, i < 4 ? 'attention' : 'good'));
  assert.deepEqual(consistency(list), [{ id: 'top.posture', name: 'Posture at the top', inRange: 8, measured: 10 }]);
});

test('practice: swings this week and consecutive weeks', () => {
  const now = new Date(2026, 10, 6, 12); // Friday
  const list = [swing(0, 7, 'good', 36), swing(1, 7, 'good', 29), swing(2, 7, 'good', 22)];
  const p = practice(list, now);
  assert.equal(p.thisWeek, 1);
  assert.equal(p.weekStreak, 3);
});

const rank = (over: Partial<RankInfo> = {}): RankInfo => ({
  rank: { index: 3, name: 'Silver III', tier: 'Silver', division: 'III' }, rp: 260, rating: 7.4, next: null,
  personal_best: 8.1, eligible_swings: 6, provisional: false, provisional_remaining: 0,
  events: [{ swing_id: 'a', date: '2026-10-01', kind: 'not_counted', reason: 'duplicate' },
           { swing_id: 'b', date: '2026-10-02', kind: 'not_counted', reason: 'daily_cap' },
           { swing_id: 'c', date: '2026-10-02', kind: 'not_counted', reason: 'duplicate' }],
  timeline: [], achievements: { first_swing: '2026-10-01', 'fixed:top.posture': '2026-10-05' },
  disclaimer: '', config: { points: {}, daily_cap: 5, tier_rating: {}, division_rp: [] }, ...over,
});

test('not-counted reasons and achievements are summarised', () => {
  assert.deepEqual(notCounted(rank()), { duplicate: 2, dailyCap: 1 });
  assert.deepEqual(achievements(rank()).map((a) => a.title), ['Posture at the top back in range', 'First counted swing']);
});

test('points complete but score short: the bar is not full and the message names the gap', () => {
  const r = rank({
    rank: { index: 8, name: 'Gold I', tier: 'Gold', division: 'I' }, rp: 2300, rating: 8.3,
    next: { name: 'Platinum III', rp_needed: 0, rp_progress: 1, rating_needed: 8.6, rp_required: 1400, rp_current: 2300,
            points_met: true, rating_required: 8.6, rating_current: 8.3, rating_met: false, rating_progress: 0.5,
            rating_gap: 0.3, progress: 0.5, ready: false },
  });
  const o = nextObjective(r)!;
  assert.ok(o.overall < 1);
  assert.equal(o.points.met, true);
  assert.deepEqual(o.score, { met: false, current: 8.3, required: 8.6, progress: 0.5 });
  assert.equal(o.message, 'Your points are ready. Improve your typical swing score by 0.3 to reach Platinum III.');
});

test('within a tier only points are asked for', () => {
  const r = rank({ next: { name: 'Silver II', rp_needed: 40, rp_progress: 0.6, rating_needed: null, rp_required: 380,
    points_met: false, rating_required: 7.0, rating_current: 7.4, rating_met: true, rating_progress: 1, progress: 0.6 } });
  const o = nextObjective(r)!;
  assert.equal(o.message, 'Earn 120 more points to reach Silver II.');
  assert.equal(o.overall, 0.6);
});

test('older servers (no breakdown) still give an honest objective', () => {
  const o = nextObjective(rank({ rp: 900, rating: 7.6, next: { name: 'Gold III', rp_needed: 0, rp_progress: 1, rating_needed: 8.0 } }))!;
  assert.ok(o.overall < 1);
  assert.match(o.message, /Improve your typical swing score by 0\.4 to reach Gold III/);
});

test('rules v2: score met once still needs the next weekly check, and tenure is spelled out', () => {
  const r = rank({
    rank: { index: 8, name: 'Gold I', tier: 'Gold', division: 'I' }, rp: 1500, rating: 8.5,
    next: { name: 'Platinum III', rp_needed: 0, rp_progress: 1, rating_needed: 8.4, rp_required: 1400, points_met: true,
            rating_required: 8.4, rating_current: 8.5, rating_met: true, rating_progress: 1, tier_open: false,
            weeks_required: 2, weeks_met: 1, next_evaluation: '2026-11-16T00:00:00+00:00', progress: 0.5,
            tenure: { weeks_required: 6, weeks_done: 4, swings_required: 40, swings_done: 31, met: false } },
  });
  const o = nextObjective(r)!;
  assert.deepEqual(o.weeks, { met: 1, required: 2, nextCheck: '2026-11-16T00:00:00+00:00' });
  assert.equal(o.tenure?.met, false);
  assert.equal(o.message,
    'Keep your typical score there at the next weekly check, keep practising (2 more weeks, 9 more eligible swings) to reach Platinum III.');
  assert.ok(o.overall < 1);
});

test('rules v2: within an open tier only points are asked for', () => {
  const r = rank({ next: { name: 'Gold II', rp_needed: 120, rp_progress: 0.4, rating_needed: null, rp_required: 900,
    points_met: false, rating_required: 7.8, rating_current: 8.1, rating_met: true, tier_open: true, weeks_required: 2,
    weeks_met: 2, tenure: null, progress: 0.4 }, rp: 780 });
  const o = nextObjective(r)!;
  assert.equal(o.score, null);
  assert.equal(o.message, 'Earn 120 more points to reach Gold II.');
});

test('celebrate on the first view and on a rise, never on a repeat', () => {
  const p = { rank: rank(), swings: [], pending: 0, personal_bests: { score: null, by_club: {} } } as Progress;
  assert.equal(shouldCelebrate(p, null), true);
  assert.equal(shouldCelebrate(p, 3), false);
  assert.equal(shouldCelebrate(p, 2), true);
  assert.equal(shouldCelebrate({ ...p, rank: rank({ eligible_swings: 0 }) }, null), false);
});
