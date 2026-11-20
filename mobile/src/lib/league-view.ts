// Pure helpers for the League screen, from GET /v1/league (no React; unit-tested). The server decides every
// number; these only phrase it.
import type { League, LeagueMember, LeagueOutcome, LeagueResult, LeagueSource } from './types';

export function ordinal(n: number): string {
  const tens = n % 100;
  if (tens >= 11 && tens <= 13) return `${n}th`;
  return `${n}${({ 1: 'st', 2: 'nd', 3: 'rd' } as Record<number, string>)[n % 10] ?? 'th'}`;
}

export function outcomeText(r: Pick<LeagueResult, 'outcome' | 'division'> & { new_division?: string }): string {
  const to = r.new_division ?? r.division;
  return ({
    promoted: `Promoted to ${to}`,
    relegated: `Moved down to ${to}`,
    medal: 'Diamond medal',
    stayed: `Stayed in ${r.division}`,
  } as Record<LeagueOutcome, string>)[r.outcome];
}

/** Where you stand this week, in one sentence. */
export function standingLine(league: League): string | null {
  const g = league.group;
  const you = league.you;
  if (!g || !you) return null;
  const top = g.division === 'Diamond';
  if (you.lp === 0) {
    return g.division === 'Bronze'
      ? 'Analyse a swing to get on the board this week.'
      : `Analyse a swing this week: 0 LP at the reset means moving down from ${g.division}.`;
  }
  if (you.position <= g.promote) {
    return top ? `You're in the medal places (top ${g.promote}).` : `You're in the promotion zone (top ${g.promote}).`;
  }
  const line = g.members.find((m) => m.position === g.promote);
  const gap = line ? line.lp - you.lp + 1 : null;
  const target = top ? 'the medal places' : 'the promotion zone';
  if (g.relegate && you.position > g.size - g.relegate) {
    const safe = g.members.find((m) => m.position === g.size - g.relegate);
    const need = safe ? safe.lp - you.lp + 1 : null;
    return need !== null ? `You're in the relegation zone. About ${need} LP lifts you out of it.` : "You're in the relegation zone.";
  }
  return gap !== null ? `About ${gap} LP to reach ${target}.` : null;
}

/** Positions after which the table draws a promotion or relegation line. */
export function zoneLines(league: League): { promoteAfter: number | null; relegateAfter: number | null } {
  const g = league.group;
  if (!g) return { promoteAfter: null, relegateAfter: null };
  return { promoteAfter: g.promote, relegateAfter: g.relegate ? g.size - g.relegate : null };
}

export function timeLeft(end: string, now: Date = new Date()): string {
  const ms = new Date(end).getTime() - now.getTime();
  if (ms <= 0) return 'Results are being finalised';
  const hours = Math.floor(ms / 3_600_000);
  if (hours >= 48) return `${Math.floor(hours / 24)} days left`;
  if (hours >= 1) return `${hours} ${hours === 1 ? 'hour' : 'hours'} left`;
  const minutes = Math.max(1, Math.floor(ms / 60_000));
  return `${minutes} ${minutes === 1 ? 'minute' : 'minutes'} left`;
}

export interface BreakdownRow { source: LeagueSource; label: string; lp: number; max: number; detail: string }

/** Your week, one row per LP source, with how much of each limit is used. */
export function breakdown(league: League): BreakdownRow[] {
  const you = league.you;
  if (!you) return [];
  const lp = league.rules.lp;
  const days = Math.min(you.practice_days, lp.practice_days_cap);
  const swings = Math.round(you.breakdown.swing / lp.swing);
  const fixes = Math.round(you.breakdown.fix / lp.fix);
  const minSwings = lp.improve_min_swings;
  return [
    { source: 'practice_day', label: 'Practice days', lp: you.breakdown.practice_day, max: lp.practice_day * lp.practice_days_cap,
      detail: `${days} of ${lp.practice_days_cap} days` },
    { source: 'swing', label: 'Counted swings', lp: you.breakdown.swing, max: lp.swing * lp.week_swings_cap,
      detail: `${swings} of ${lp.week_swings_cap} swings` },
    { source: 'improvement', label: 'Improvement (pending)', lp: you.breakdown.improvement, max: lp.improve_cap,
      detail: you.eligible_swings < minSwings
        ? `Needs ${minSwings - you.eligible_swings} more eligible ${minSwings - you.eligible_swings === 1 ? 'swing' : 'swings'}`
        : `Week median ${you.week_median?.toFixed(1) ?? '–'} vs your ${you.baseline.toFixed(1)} · added at Monday's reset` },
    { source: 'fix', label: 'Checks back in range', lp: you.breakdown.fix, max: lp.fix * lp.fix_cap,
      detail: `${fixes} of ${lp.fix_cap}` },
    { source: 'consistent_week', label: 'Consistent week (pending)', lp: you.breakdown.consistent_week, max: lp.consistent_week,
      detail: you.breakdown.consistent_week ? "On track · added at Monday's reset" : `${lp.consistent_days}+ practice days and a steady week` },
  ];
}

export function rivalCountText(n: number): string {
  return n === 1 ? '1 practice rival in this league' : `${n} practice rivals in this league`;
}

export function memberLabel(m: LeagueMember, rivalLabel: string): string {
  const who = m.is_you ? `${m.name} (you)` : m.name;
  return `${ordinal(m.position)}, ${who}${m.is_rival ? `, ${rivalLabel.toLowerCase()}, computer-generated` : ''}, ${m.lp} league points`;
}

/** "Sophie Anderson" -> "SA"; "de Vries" style particles are skipped. */
export function initialsOf(name: string): string {
  const parts = name.replace(/-/g, ' ').split(/\s+/).filter((p) => /^\p{L}/u.test(p) && p[0] === p[0].toUpperCase());
  if (!parts.length) return name.trim().slice(0, 1).toUpperCase() || '?';
  return (parts[0][0] + (parts.length > 1 ? parts[parts.length - 1][0] : '')).toUpperCase();
}

/** A stable colour slot (0..AVATAR_SLOTS-1) for a name: the same person always gets the same colour. */
export const AVATAR_SLOTS = 8;
export function avatarSlot(name: string): number {
  let h = 2166136261;
  for (const ch of name) h = Math.imul(h ^ ch.codePointAt(0)!, 16777619) >>> 0;
  return h % AVATAR_SLOTS;
}

/** Up to two short, factual lines from the real standings (positions only: never names). */
export function insights(league: League, now: Date = new Date()): string[] {
  const g = league.group;
  const you = league.you;
  if (!g || !you) return [];
  const out: string[] = [];
  const top = g.division === 'Diamond';
  const above = g.members.find((m) => m.position === you.position - 1);
  const line = g.members.find((m) => m.position === g.promote);
  if (you.lp === 0) {
    out.push(g.division === 'Bronze' ? 'Analyse a swing to get on the board this week.'
      : `Analyse a swing this week: 0 LP at the reset means moving down from ${g.division}.`);
  } else if (you.position === 1) {
    out.push("You're top of the league.");
  } else if (you.position <= g.promote) {
    const gap = above ? above.lp - you.lp + 1 : null;
    out.push(top ? "You're in the medal places." : "You're in the promotion zone.");
    if (gap !== null && gap <= 25) out.push(`${gap} LP to move up to ${ordinal(you.position - 1)}.`);
  } else if (g.relegate && you.position > g.size - g.relegate) {
    const safe = g.members.find((m) => m.position === g.size - g.relegate);
    out.push(safe ? `${safe.lp - you.lp + 1} LP to climb out of the relegation zone.` : "You're in the relegation zone.");
  } else {
    if (line) out.push(`${line.lp - you.lp + 1} LP to reach the ${top ? 'medal places' : 'promotion zone'}.`);
    if (above && above.position !== g.promote) out.push(`You're ${above.lp - you.lp + 1} LP away from ${ordinal(above.position)} place.`);
  }
  const days = Math.ceil((new Date(league.week.end).getTime() - now.getTime()) / 86_400_000);
  if (out.length < 2 && days >= 1 && days <= 7) out.push(days === 1 ? 'Last day of this week\'s league.' : `${days} days left in this week's league.`);
  return out.slice(0, 2);
}

/** How many places you moved since you last looked this week (positive = up). */
export function movement(previous: number | null, current: number): number {
  return previous === null ? 0 : previous - current;
}

/** The "how it works" lines, built from the server's own rule values. */
export function rulesLines(league: League): string[] {
  const lp = league.rules.lp;
  return [
    `+${lp.practice_day} LP for each practice day, up to ${lp.practice_days_cap} days a week.`,
    `+${lp.swing} LP for each counted swing, up to ${lp.week_swings_cap} a week (the first swings of each day count).`,
    `Up to +${lp.improve_cap} LP when this week's typical score beats your recent level (judged on the whole week, from ${lp.improve_min_swings}+ eligible swings).`,
    'Weekly bonuses (improvement and consistent week) show as pending and are added at Monday\'s reset, so totals in the table never go down during the week.',
    `+${lp.fix} LP when a check that was out of range is back in range on two swings in a row (up to ${lp.fix_cap} a week).`,
    `+${lp.consistent_week} LP for a consistent week: ${lp.consistent_days}+ practice days and steady scores.`,
    'Re-uploading the same video never earns points, and deleting swings can only lower your total.',
    'Until more golfers join, empty places are filled by practice rivals: computer-generated competitors, marked with a dashed ring, that earn points under exactly the same rules and limits.',
    'The week resets on Monday at 00:00 UTC. Positions decide promotion and relegation; ties go to whoever got there first.',
  ];
}
