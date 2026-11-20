import assert from 'node:assert/strict';
import { test } from 'node:test';

import {
  AVATAR_SLOTS, avatarSlot, breakdown, initialsOf, insights, memberLabel, movement, ordinal, outcomeText, rivalCountText,
  standingLine, timeLeft, zoneLines,
} from '../src/lib/league-view.ts';
import type { League, LeagueMember } from '../src/lib/types.ts';

const RULES = {
  lp: { practice_day: 6, practice_days_cap: 3, swing: 2, week_swings_cap: 9, improve_per_point: 80, improve_cap: 60,
        improve_min_swings: 3, fix: 15, fix_cap: 3, consistent_week: 20, consistent_days: 3, baseline_swings: 60, baseline_min: 5 },
  group_size: 20, grace_minutes: 30, divisions: [],
};

function league(division: 'Bronze' | 'Silver' | 'Diamond', youPos: number, youLp: number): League {
  const promote = { Bronze: 7, Silver: 6, Diamond: 3 }[division];
  const relegate = { Bronze: 0, Silver: 3, Diamond: 5 }[division];
  const members: LeagueMember[] = Array.from({ length: 20 }, (_, i) => ({
    position: i + 1, name: i + 1 === youPos ? 'Harry' : `Rival · R${i}`, lp: i + 1 === youPos ? youLp : 100 - i * 5,
    is_rival: i + 1 !== youPos, is_you: i + 1 === youPos, zone: 'safe',
  }));
  return {
    week: { start: '2026-11-16T00:00:00+00:00', end: '2026-11-23T00:00:00+00:00', closes_at: '2026-11-23T00:30:00+00:00' },
    rules: RULES, rival_label: 'Practice rival', rival_profile_line: 'Simulated', joined: true,
    profile: { display_name: 'Harry', division, joined_at: '' },
    group: { division, size: 20, rival_count: 19, promote, relegate, members },
    you: { position: youPos, lp: youLp, breakdown: { practice_day: 12, swing: 8, improvement: 0, fix: 15, consistent_week: 0 },
           practice_days: 2, counted_swings: 4, eligible_swings: 2, week_median: 7.1, baseline: 7.0, baseline_default: true,
           fixed_checks: ['top.posture'], zone: 'safe' },
  };
}

test('ordinals', () => {
  assert.deepEqual([1, 2, 3, 4, 11, 12, 13, 21, 22, 112].map(ordinal), ['1st', '2nd', '3rd', '4th', '11th', '12th', '13th', '21st', '22nd', '112th']);
});

test('outcomes read naturally', () => {
  assert.equal(outcomeText({ outcome: 'promoted', division: 'Bronze', new_division: 'Silver' }), 'Promoted to Silver');
  assert.equal(outcomeText({ outcome: 'relegated', division: 'Gold', new_division: 'Silver' }), 'Moved down to Silver');
  assert.equal(outcomeText({ outcome: 'stayed', division: 'Gold' }), 'Stayed in Gold');
  assert.equal(outcomeText({ outcome: 'medal', division: 'Diamond' }), 'Diamond medal');
});

test('standing line: promotion zone, gap, relegation, zero LP', () => {
  assert.match(standingLine(league('Bronze', 3, 95))!, /promotion zone \(top 7\)/);
  assert.equal(standingLine(league('Bronze', 10, 50)), 'About 21 LP to reach the promotion zone.');   // 7th has 70
  assert.match(standingLine(league('Silver', 19, 5))!, /relegation zone/);
  assert.match(standingLine(league('Silver', 20, 0))!, /0 LP at the reset/);
  assert.match(standingLine(league('Bronze', 20, 0))!, /get on the board/);
  assert.match(standingLine(league('Diamond', 2, 99))!, /medal places/);
});

test('zone lines follow the division', () => {
  assert.deepEqual(zoneLines(league('Bronze', 1, 1)), { promoteAfter: 7, relegateAfter: null });
  assert.deepEqual(zoneLines(league('Silver', 1, 1)), { promoteAfter: 6, relegateAfter: 17 });
});

test('breakdown rows show the 3-day practice cap and other limits', () => {
  const rows = breakdown(league('Bronze', 5, 35));
  assert.deepEqual(rows.map((r) => r.detail), ['2 of 3 days', '4 of 9 swings', 'Needs 1 more eligible swing', '1 of 3',
    '3+ practice days and a steady week']);
  assert.equal(rows[0].max, 18);
  assert.equal(rows.reduce((s, r) => s + r.lp, 0), 35);
  assert.deepEqual(rows.filter((r) => r.label.endsWith('(pending)')).map((r) => r.source), ['improvement', 'consistent_week']);
});

test('time left and labels', () => {
  const end = '2026-11-23T00:00:00Z';
  assert.equal(timeLeft(end, new Date('2026-11-19T00:00:00Z')), '4 days left');
  assert.equal(timeLeft(end, new Date('2026-11-22T19:00:00Z')), '5 hours left');
  assert.equal(timeLeft(end, new Date('2026-11-22T23:59:00Z')), '1 minute left');
  assert.equal(timeLeft(end, new Date('2026-11-23T00:10:00Z')), 'Results are being finalised');
  assert.equal(rivalCountText(1), '1 practice rival in this league');
  assert.equal(rivalCountText(17), '17 practice rivals in this league');
  const m: LeagueMember = { position: 2, name: 'Callum Stewart', lp: 40, is_rival: true, is_you: false, zone: 'promote' };
  assert.equal(memberLabel(m, 'Practice rival'), '2nd, Callum Stewart, practice rival, computer-generated, 40 league points');
  assert.equal(memberLabel({ ...m, is_rival: false, is_you: true, name: 'Harry' }, 'Practice rival'), '2nd, Harry (you), 40 league points');
});

test('initials and stable avatar colours', () => {
  assert.equal(initialsOf('Sophie Anderson'), 'SA');
  assert.equal(initialsOf('Henrik de Vries'), 'HV');
  assert.equal(initialsOf("Sean O'Brien"), 'SO');
  assert.equal(initialsOf('harry'), 'H');
  assert.equal(initialsOf('Mary-Jane Smith'), 'MS');
  assert.equal(avatarSlot('James Wilson'), avatarSlot('James Wilson'));
  const slots = new Set(['James Wilson', 'Emma Robertson', 'Daniel Fraser', 'Ryan Mitchell', 'Ben Walker', 'Olivia Martin',
    'Callum Stewart', 'Jack Henderson', 'Andrew Campbell', 'Sophie Anderson'].map(avatarSlot));
  assert.ok(slots.size >= 5 && [...slots].every((s) => s >= 0 && s < AVATAR_SLOTS));
});

test('insights are computed from the standings, by position only', () => {
  const now = new Date('2026-11-19T12:00:00Z');                  // Thursday
  const mid = league('Bronze', 10, 50);                            // 7th has 70, 9th has 60
  assert.deepEqual(insights(mid, now), ['21 LP to reach the promotion zone.', "You're 11 LP away from 9th place."]);
  const inZone = league('Bronze', 5, 82);                          // 4th has 85
  assert.deepEqual(insights(inZone, now), ["You're in the promotion zone.", '4 LP to move up to 4th.']);
  assert.deepEqual(insights(league('Bronze', 1, 200), now), ["You're top of the league.", "4 days left in this week's league."]);
  assert.match(insights(league('Silver', 19, 5), now)[0], /climb out of the relegation zone/);
  assert.match(insights(league('Diamond', 2, 99), now)[0], /medal places/);
  for (const line of insights(mid, now)) assert.ok(!/Rival|James|Callum/.test(line));   // never names another player
});

test('movement since last look', () => {
  assert.equal(movement(null, 5), 0);
  assert.equal(movement(8, 5), 3);
  assert.equal(movement(4, 6), -2);
});
