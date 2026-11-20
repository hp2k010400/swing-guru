// Pure helpers for the Progress screen, from GET /v1/progress (no React; unit-tested).
import type { Progress, ProgressSwing, RankInfo } from './types';

const CHECK_NAMES: Record<string, string> = {
  'address.torso_angle': 'Hip hinge', 'address.trail_knee': 'Knee bend', 'address.arm_hang': 'Arm hang',
  'mid_back.shaft': 'Plane halfway back', 'top.posture': 'Posture at the top', 'downswing.shaft': 'Plane coming down',
  'impact.pelvis': 'Hips at impact', 'impact.head': 'Head movement',
};

export function checkName(id: string): string {
  return CHECK_NAMES[id] ?? id;
}

function median(values: number[]): number | null {
  if (!values.length) return null;
  const s = [...values].sort((a, b) => a - b);
  const m = Math.floor(s.length / 2);
  return s.length % 2 ? s[m] : (s[m - 1] + s[m]) / 2;
}

/** The last `n` scored swings in true chronological order (imported history included). */
export function scoredSeries(swings: ProgressSwing[], n: number): (ProgressSwing & { score: number })[] {
  return swings.filter((s): s is ProgressSwing & { score: number } => s.score !== null)
    .sort((a, b) => a.created_at.localeCompare(b.created_at)).slice(-n);
}

/** Median of each run of `n` consecutive values (null until there are `n`): a calm trend line. */
export function rollingMedian(values: number[], n: number): (number | null)[] {
  return values.map((_, i) => (i + 1 < n ? null : median(values.slice(i + 1 - n, i + 1))));
}

/** Median score of the last `n` scored swings vs the `n` before: the recent trend, or null if too few. */
export function trend(swings: ProgressSwing[], n = 5): { recent: number; before: number; delta: number } | null {
  const scored = swings.filter((s) => s.score !== null).map((s) => s.score as number);
  if (scored.length < 2 * n) return null;
  const recent = median(scored.slice(-n))!;
  const before = median(scored.slice(-2 * n, -n))!;
  return { recent, before, delta: Math.round((recent - before) * 10) / 10 };
}

/** Per check: how often it was in range over the last `window` swings that measured it. */
export function consistency(swings: ProgressSwing[], window = 10): { id: string; name: string; inRange: number; measured: number }[] {
  const order = Object.keys(CHECK_NAMES);
  const rows = new Map<string, { inRange: number; measured: number }>();
  for (const id of order) {
    const recent = swings.flatMap((s) => s.checks.filter((c) => c.id === id)).slice(-window);
    if (recent.length) rows.set(id, { inRange: recent.filter((c) => c.status === 'good').length, measured: recent.length });
  }
  return order.filter((id) => rows.has(id)).map((id) => ({ id, name: checkName(id), ...rows.get(id)! }));
}

/** Monday 00:00 (local) of the week containing `d`, stepped by calendar days so clock changes can't
 *  shift it (a fixed 7 x 24 h step lands on Sunday 23:00 across the October clock change). */
function weekStart(d: Date, weeksBack = 0): number {
  return new Date(d.getFullYear(), d.getMonth(), d.getDate() - ((d.getDay() + 6) % 7) - 7 * weeksBack).getTime();
}

export function practice(swings: ProgressSwing[], now = new Date()): { thisWeek: number; weekStreak: number; total: number } {
  const weeks = new Map<number, number>();
  for (const s of swings) {
    const w = weekStart(new Date(s.created_at));
    weeks.set(w, (weeks.get(w) ?? 0) + 1);
  }
  const thisWeek = weeks.get(weekStart(now)) ?? 0;
  let back = thisWeek ? 0 : 1;
  let weekStreak = 0;
  while (weeks.has(weekStart(now, back))) {
    weekStreak += 1;
    back += 1;
  }
  return { thisWeek, weekStreak, total: swings.length };
}

export function achievementTitle(key: string): string {
  if (key === 'first_swing') return 'First counted swing';
  if (key === 'personal_best') return 'New personal best';
  if (key === 'all_in_range') return 'Every check in range';
  if (key.startsWith('fixed:')) return `${checkName(key.slice(6))} back in range`;
  return key;
}

export function achievements(rank: RankInfo): { key: string; title: string; date: string }[] {
  return Object.entries(rank.achievements)
    .map(([key, date]) => ({ key, title: achievementTitle(key), date }))
    .sort((a, b) => b.date.localeCompare(a.date));
}

export function notCounted(rank: RankInfo): { duplicate: number; dailyCap: number } {
  let duplicate = 0, dailyCap = 0;
  for (const e of rank.events) {
    if (e.kind !== 'not_counted') continue;
    if (e.reason === 'duplicate') duplicate += 1;
    if (e.reason === 'daily_cap') dailyCap += 1;
  }
  return { duplicate, dailyCap };
}

export interface NextObjective {
  overall: number;                     // 0-1: never full while a requirement is open
  points: { met: boolean; current: number; required: number; progress: number };
  score: { met: boolean; current: number | null; required: number; progress: number } | null;   // null: no score gate
  /** Rank rules v2: consecutive weekly checks the score gate has met (null when the tier is already open). */
  weeks: { met: number; required: number; nextCheck: string | null } | null;
  tenure: { met: boolean; weeksDone: number; weeksRequired: number; swingsDone: number; swingsRequired: number } | null;
  message: string;
}

function v2Objective(rank: RankInfo): NextObjective {
  const n = rank.next!;
  const required = n.rp_required ?? rank.rp + n.rp_needed;
  const pointsMet = n.points_met ?? n.rp_needed <= 0;
  const pts = Math.max(0, required - rank.rp);
  const open = Boolean(n.tier_open);
  const gate = n.rating_required ?? null;
  const current = n.rating_current ?? rank.rating;
  const scoreNow = Boolean(n.rating_met);
  const weeksRequired = n.weeks_required ?? 2;
  const weeksMet = n.weeks_met ?? 0;
  const t = n.tenure ?? null;
  const tenure = t && !open ? { met: t.met, weeksDone: t.weeks_done, weeksRequired: t.weeks_required, swingsDone: t.swings_done, swingsRequired: t.swings_required } : null;
  const score = !open && gate ? { met: scoreNow, current, required: gate, progress: n.rating_progress ?? 0 } : null;
  const weeks = !open && gate ? { met: weeksMet, required: weeksRequired, nextCheck: n.next_evaluation ?? null } : null;
  const parts: string[] = [];
  if (!pointsMet) parts.push(`earn ${pts} more points`);
  if (score && !scoreNow && current !== null) parts.push(`raise your typical score by ${Math.max(0.1, Math.round((gate! - current) * 10) / 10).toFixed(1)} to ${gate!.toFixed(1)}`);
  if (score && !scoreNow && current === null) parts.push('build up your typical score with more eligible swings');
  if (score && weeksMet < weeksRequired) parts.push(scoreNow
    ? `keep your typical score there at ${weeksRequired - weeksMet === 1 ? 'the next weekly check' : `${weeksRequired - weeksMet} more weekly checks`}`
    : `then hold it for ${weeksRequired} weekly checks in a row`);
  if (tenure && !tenure.met) {
    const w = Math.max(0, tenure.weeksRequired - tenure.weeksDone), s = Math.max(0, tenure.swingsRequired - tenure.swingsDone);
    parts.push(`keep practising (${[w ? `${w} more week${w === 1 ? '' : 's'}` : null, s ? `${s} more eligible swing${s === 1 ? '' : 's'}` : null].filter(Boolean).join(', ')})`);
  }
  const sentence = parts.length ? parts.join(', ') : 'everything is in place: it confirms at the next weekly check';
  const message = `${sentence.charAt(0).toUpperCase()}${sentence.slice(1)} to reach ${n.name}.`.replace('to reach', parts.length ? 'to reach' : 'for');
  return {
    overall: n.progress ?? 0,
    points: { met: pointsMet, current: rank.rp, required, progress: pointsMet ? 1 : n.rp_progress },
    score, weeks, tenure, message,
  };
}

/** What stands between the golfer and the next rank, requirement by requirement (no hard-coded ranks). */
export function nextObjective(rank: RankInfo): NextObjective | null {
  const n = rank.next;
  if (!n) return null;
  if (n.weeks_required !== undefined) return v2Objective(rank);
  const required = n.rp_required ?? rank.rp + n.rp_needed;
  const pointsMet = n.points_met ?? n.rp_needed <= 0;
  const gate = n.rating_required ?? n.rating_needed ?? null;
  const scoreMet = n.rating_met ?? n.rating_needed === null;
  const current = n.rating_current ?? rank.rating;
  const scoreProgress = scoreMet ? 1 : n.rating_progress ?? 0;
  const score = gate !== null && gate > 0 && (!scoreMet || n.rating_required) ? { met: scoreMet, current, required: gate, progress: scoreProgress } : null;
  const gap = current !== null && gate !== null ? Math.max(0, Math.round((gate - current) * 10) / 10) : null;
  const pts = Math.max(0, required - rank.rp);
  let message: string;
  if (pointsMet && scoreMet) message = `Ready for ${n.name}: your next counted swing confirms it.`;
  else if (pointsMet) message = gap !== null
    ? `Your points are ready. Improve your typical swing score by ${gap.toFixed(1)} to reach ${n.name}.`
    : `Your points are ready. Analyse a few more swings to set your typical score for ${n.name}.`;
  else if (score && !scoreMet) message = `Earn ${pts} more points and raise your typical score by ${gap?.toFixed(1) ?? '–'} to reach ${n.name}.`;
  else message = `Earn ${pts} more points to reach ${n.name}.`;
  const pointsProgress = pointsMet ? 1 : n.rp_progress;
  return {
    overall: n.progress ?? Math.min(pointsProgress, score ? scoreProgress : 1),
    points: { met: pointsMet, current: rank.rp, required, progress: pointsProgress },
    score,
    weeks: null,
    tenure: null,
    message,
  };
}

/** Celebrate when the rank index rose since the last time this account saw it (or on the first view). */
export function shouldCelebrate(progress: Progress, lastSeen: number | null): boolean {
  if (!progress.rank.eligible_swings) return false;
  return lastSeen === null || progress.rank.rank.index > lastSeen;
}
