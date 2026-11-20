// Mirrors src/swingguru/service/schemas.py (API v1).
import type { Status } from '@/theme';

// The club picker (mirrors app_api.CLUBS). "I'm not sure" is stored as null. Hybrid / Irons / Wedges
// are the first picker's groups, still valid on older swings.
export const CLUB_GROUPS = [
  { label: 'Woods', clubs: ['Driver', '3 Wood', '5 Wood', '7 Wood'] },
  { label: 'Hybrids', clubs: ['2 Hybrid', '3 Hybrid', '4 Hybrid', '5 Hybrid'] },
  { label: 'Irons', clubs: ['2 Iron', '3 Iron', '4 Iron', '5 Iron', '6 Iron', '7 Iron', '8 Iron', '9 Iron'] },
  { label: 'Wedges', clubs: ['Pitching Wedge', 'Gap Wedge', 'Sand Wedge', 'Lob Wedge'] },
] as const;

export type Club = (typeof CLUB_GROUPS)[number]['clubs'][number] | 'Other' | 'Hybrid' | 'Irons' | 'Wedges';

export type JobStatus = 'awaiting_upload' | 'queued' | 'processing' | 'complete' | 'failed';

export interface Stage {
  id: string;
  label: string;
  state: 'pending' | 'active' | 'done';
}

export interface ErrorInfo {
  code: string;
  title: string;
  advice: string;
}

export interface Job {
  api_version: string;
  id: string;
  status: JobStatus;
  stages: Stage[];
  error: ErrorInfo | null;
  notice: string | null;
  swing_id: string | null;
  created_at: string;
  updated_at: string;
}

export interface UploadInstructions {
  method: 'PUT';
  url: string;
  headers: Record<string, string>;
  expires_in_s: number;
}

export interface CreateJobResponse {
  api_version: string;
  job: Job;
  upload: UploadInstructions;
}

export interface Counts {
  good: number;
  attention: number;
  problem: number;
  total: number;
}

/** One check's part of the Swing Score (swingguru/scoring). */
export interface ScoreCheck {
  id: string;
  phase: string;
  name: string;
  measurement: number;
  unit: string;
  status: Status;
  label: string | null;
  confidence: 'HIGH' | 'MEDIUM';
  estimated: boolean;
  points: number;
  weight: number;
  share: number;
}

/** The versioned Swing Score for one swing. `total` is null when too few checks were measured reliably. */
export interface SwingScore {
  available: boolean;
  total: number | null;
  version: string;
  source: 'model' | 'stub' | 'none';
  scale: number;
  measured: number;
  eligible: number;
  coverage: number;
  minimum: number;
  confidence: { high: number; medium: number; label: 'HIGH' | 'MEDIUM' | null };
  checks: ScoreCheck[];
  // weight/points: present from swing_score_v2, where an unmeasured check counts as neutral at reduced weight.
  excluded: { id: string; phase: string; name: string; reason: string; weight?: number; points?: number }[];
  method: string;
  reason: string | null;
}

export type ScoreBrief = Pick<SwingScore, 'available' | 'total' | 'version' | 'measured' | 'eligible'>;

export interface SwingListItem {
  id: string;
  captured_at: string | null;
  created_at: string;
  club: Club | null;
  counts: Counts | null;
  summary_text: string | null;
  thumb_url: string | null;
  notice: string | null;
  score?: ScoreBrief | null;
}

/** Plain-language card for one checkpoint (swingguru/coaching.py). Absent from older servers. */
export interface Coaching {
  headline: string;
  what: string;
  why: string;
  fix: string | null;
  side: 'good' | 'low' | 'high';
  /** How much it matters (coaching v2): "a small difference…" / "a clear difference…"; null when in range. */
  note?: string | null;
  low_trust?: boolean;
  version: number;
  status: string;
}

export interface Checkpoint {
  id: string;
  phase: string;
  phase_label: string;
  name: string;
  label: string;
  status: Status;
  support: string | null;
  value: string | null;
  drills?: string[];
  coaching?: Coaching | null;
}

export interface BreakdownRow {
  id: string;
  kind: 'check' | 'withheld' | 'info';
  name: string;
  label?: string;
  status: Status;
  value?: string | null;
  support?: string | null;
  reason?: string;
  drills?: string[];
  coaching?: Coaching | null;
}

export interface Drill {
  id: string;
  name: string;
  checkpoint: string;
  phase_label: string;
  minutes: number;
  why: string;
  steps: string[];
  reps: string;
  status: string;
  /** An original, approved demonstration video; null until one exists (never a placeholder). */
  video?: string | null;
  /** The checks this drill trains (1.0.1 servers). */
  targets?: { id: string; name: string; phase: string; phase_label: string }[];
  /** An externally hosted demonstration, played in the provider's official player (1.0.1 servers). */
  demo_video?: DemoVideo | null;
}

export interface DemoVideo {
  provider: 'youtube';
  video_id: string;
  start_s: number;
  title: string;
  creator: string;
  watch_url: string;
  embed_url: string;
  /** Not loaded by the app (nothing is requested from YouTube before play); kept for the web viewer. */
  thumbnail_url: string;
  suitability: string;
  duration_s?: number | null;
  /** "16:9" (default) or "9:16" for vertical videos such as Shorts. */
  aspect?: '16:9' | '9:16' | null;
}

export interface SwingDetail {
  api_version: string;
  id: string;
  captured_at: string | null;
  created_at: string;
  club: Club | null;
  clubs: Club[];
  notice: string | null;
  analysis_version: string | null;
  headline: Counts & { phases_analysed: number };
  summary_text: string;
  checkpoints: Checkpoint[];
  withheld: BreakdownRow[];
  breakdown: { phase: string; label: string; rows: BreakdownRow[] }[];
  recommended: { drill: Drill; checkpoint: Pick<Checkpoint, 'id' | 'name' | 'phase_label' | 'label' | 'status'> }[];
  hand_position: { value: string; text: string } | null;
  camera: { moved: boolean; corrected_phases: string[] };
  score: SwingScore;
  focus: { available: boolean; source: string; message: string; title: string | null; label: string | null; status: Status | null; drills?: Drill[] };
  thumb_url: string | null;
  replay: { viewer_url: string; analysis_url: string; video_url: string; expires_in_s: number };
}

/** GET /v1/progress (swingguru/ranks.py + swing_history.py). */
export interface RankInfo {
  rank: { index: number; name: string; tier: 'Bronze' | 'Silver' | 'Gold' | 'Platinum' | 'Diamond'; division: 'III' | 'II' | 'I' };
  rp: number;
  rating: number | null;
  next: {
    name: string; rp_needed: number; rp_progress: number; rating_needed: number | null;
    // Each requirement on its own (servers from Build 17); `progress` = the weaker of the two.
    rp_required?: number; rp_current?: number; points_met?: boolean;
    rating_required?: number | null; rating_current?: number | null; rating_met?: boolean;
    rating_progress?: number; rating_gap?: number | null; progress?: number; ready?: boolean;
    // Rank rules v2: tier promotions at the weekly check, after 2 consecutive qualifying weeks, plus tenure.
    tier_open?: boolean; weeks_required?: number; weeks_met?: number; next_evaluation?: string | null;
    tenure?: { weeks_required: number; weeks_done: number; swings_required: number; swings_done: number; met: boolean } | null;
  } | null;
  eligibility?: { eligible: number; practice_only: number; duplicate: number; no_score: number };
  next_evaluation?: string | null;
  personal_best: number | null;
  eligible_swings: number;
  provisional: boolean;
  provisional_remaining: number;
  events: ({ swing_id: string; date: string } & (
    | { kind: 'counted'; rp: number; reasons: string[] }
    | { kind: 'not_counted'; reason: 'no_score' | 'duplicate' | 'daily_cap' }
    | { kind: 'rank_up'; from: string; to: string }))[];
  timeline: { date: string; rp: number; rating: number | null; rank: string }[];
  achievements: Record<string, string>;
  disclaimer: string;
  config: {
    points: Record<string, number>; daily_cap: number; tier_rating: Record<string, number>; division_rp: number[];
    practice_week_swings?: number; provisional_swings?: number; rating_window?: number;
    day_swings?: number[]; rating_percentile?: number; rating_min_swings?: number; gate_weeks?: number;
    min_measured?: number; min_long_side?: number;
  };
}

export interface ProgressSwing {
  id: string;
  created_at: string;
  club: Club | null;
  score: number | null;
  checks: { id: string; status: Status; value: number; unit: string }[];
}

export interface Progress {
  swings: ProgressSwing[];
  pending: number;
  rank: RankInfo;
  personal_bests: { score: { swing_id: string; score: number; date: string } | null; by_club: Record<string, { swing_id: string; score: number; date: string }> };
}

/** GET /v1/league (swingguru/league/service.py; staging from Build 20). */
export type LeagueDivision = 'Bronze' | 'Silver' | 'Gold' | 'Platinum' | 'Diamond';
export type LeagueZone = 'promote' | 'medal' | 'safe' | 'relegate';
export type LeagueOutcome = 'promoted' | 'stayed' | 'relegated' | 'medal';
export type LeagueSource = 'practice_day' | 'swing' | 'improvement' | 'fix' | 'consistent_week';

export interface LeagueMember { position: number; name: string; lp: number; is_rival: boolean; is_you: boolean; zone: LeagueZone }

export interface LeagueRules {
  lp: Record<string, number>;
  group_size: number;
  grace_minutes: number;
  divisions: { name: LeagueDivision; day_swings: number; baseline_quantile: number; promote: number; relegate: number }[];
}

export interface LeagueResult { week_start: string; division: LeagueDivision; position: number; lp: number; outcome: LeagueOutcome }

export interface League {
  week: { start: string; end: string; closes_at: string };
  rules: LeagueRules;
  rival_label: string;
  rival_profile_line: string;
  joined: boolean;
  // Not joined yet.
  counted_swings?: number;
  join_min_swings?: number;
  can_join?: boolean;
  // Joined.
  profile?: { display_name: string; division: LeagueDivision; joined_at: string };
  group?: { division: LeagueDivision; size: number; rival_count: number; promote: number; relegate: number; members: LeagueMember[] };
  you?: {
    /** `lp` = confirmed points (what the table ranks on); `pending` = weekly bonuses added at Monday's reset. */
    position: number; lp: number; pending?: number; breakdown: Record<LeagueSource, number>; practice_days: number; counted_swings: number;
    eligible_swings: number; week_median: number | null; baseline: number; baseline_default: boolean; fixed_checks: string[];
    zone: LeagueZone;
  } | null;
  last_result?: (LeagueResult & { new_division: LeagueDivision }) | null;
  history?: LeagueResult[];
}

export type CompareResult = 'improved' | 'worse' | 'same' | 'not_comparable';

export interface CompareSide { status: Status; label: string; value: number; unit: string; confidence: string | null; estimated: boolean }

export interface Comparison {
  score: { a: number | null; b: number | null; delta: number | null; result: CompareResult; reason: string | null };
  rows: { id: string; phase: string; phase_label: string; name: string; a: CompareSide | null; b: CompareSide | null; result: CompareResult; reason: string; threshold?: number }[];
  counts: Record<CompareResult, number>;
  notes: string[];
  duplicate: boolean;
  swings: Record<'a' | 'b', { id: string; created_at: string; club: Club | null }>;
  moments?: Record<'a' | 'b', Partial<Record<'address' | 'top' | 'impact', number>>>;
}

/** Mirrors src/swingguru/service/schemas.py BillingStatus. Always server-derived. */
// 'in_progress' means the month's free slots are held by analyses that have not finished yet (a
// wait state); 'sign_in_required' means free analyses need a durable account (an auth state).
export type BillingAccess = 'free' | 'pro' | 'exhausted' | 'in_progress' | 'sign_in_required' | 'disabled';

export interface BillingStatus {
  api_version: string;
  enabled: boolean;
  access: BillingAccess;
  /** Current UTC calendar month: charged analyses, outstanding reservations and what is left. */
  free_total: number;
  free_used: number;
  free_reserved: number;
  free_remaining: number;
  can_analyse: boolean;
  entitlement_active: boolean;
  product_id: string | null;
  expires_at: string | null;
  management_url: string | null;
  paid_allowance: string | null;
  /** Free analyses need a durable (non-anonymous) account: an auth prompt, never a paywall. */
  requires_sign_in: boolean;
  /** Half-open allowance period [start, resets_at) in UTC, ISO 8601. */
  allowance_period_start: string | null;
  allowance_resets_at: string | null;
  allowance_timezone: string;
  allowance_model: string;
}
