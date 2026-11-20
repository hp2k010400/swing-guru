-- Swing Guru billing: the free-analysis account and the per-job usage ledger behind
-- the RevenueCat subscription gate. Additive only - no historical migration changes.
--
-- Rollout notes (also in docs/agent-work/revenuecat/SETUP.md):
-- * Billing is OFF until `SG_BILLING_MODE=on` with a complete server config, so applying
--   this migration alone changes no behaviour.
-- * Grandfathering: the first time an account is touched after rollout,
--   `free_used_seed = min(free_total, count(historical completed swings))`, recorded once.
--   In-flight jobs created before rollout have no ledger row: they finish without
--   consuming a free analysis (they were queued under the old rules).
-- * The ledger has no foreign key to `public.swings`: deleting a swing (or a stuck job)
--   must never return a consumed analysis. Only `auth.users` deletion cascades.

create table if not exists public.billing_accounts (
  user_id            uuid primary key references auth.users (id) on delete cascade,
  free_total         integer not null default 3 check (free_total >= 1),
  free_used_seed     integer not null default 0 check (free_used_seed >= 0),
  grandfather_seeded boolean not null default false,
  created_at         timestamptz not null default now(),
  updated_at         timestamptz not null default now()
);

-- One row per admitted swing (the unique key is what makes a retried /start
-- idempotent). No FK to public.swings on purpose - see the rollout notes above.
create table if not exists public.billing_usage (
  swing_id   uuid primary key,
  user_id    uuid not null references auth.users (id) on delete cascade,
  kind       text not null check (kind in ('free', 'paid')),
  state      text not null check (state in ('reserved', 'settled', 'released')),
  product_id text,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  settled_at timestamptz
);

create index if not exists billing_usage_account on public.billing_usage (user_id, kind, state);

-- The mobile app never reads or writes these tables. RLS is on with no policies at
-- all, and the API exposes only the narrowly-scoped service-role functions below.
alter table public.billing_accounts enable row level security;
alter table public.billing_usage enable row level security;

revoke all on table public.billing_accounts from public, anon, authenticated;
revoke all on table public.billing_usage from public, anon, authenticated;

-- The account picture (creating the row, and seeding the grandfather count, once).
create or replace function public.billing_status(p_user_id uuid, p_free_total integer)
returns jsonb
language plpgsql
security definer
set search_path = ''
as $$
declare
  v_grant    integer := greatest(coalesce(p_free_total, 0), 1);
  v_total    integer;
  v_seed     integer;
  v_settled  integer;
  v_reserved integer;
begin
  insert into public.billing_accounts (user_id, free_total, free_used_seed, grandfather_seeded,
                                       created_at, updated_at)
  values (
    p_user_id,
    v_grant,
    least(v_grant, coalesce((select count(*) from public.swings s
                              where s.user_id = p_user_id and s.status = 'complete'), 0)),
    true, now(), now())
  on conflict (user_id) do update
    set free_total = excluded.free_total, updated_at = now();

  select a.free_total, a.free_used_seed into v_total, v_seed
    from public.billing_accounts a where a.user_id = p_user_id;

  select count(*) into v_settled from public.billing_usage u
   where u.user_id = p_user_id and u.kind = 'free' and u.state = 'settled';
  select count(*) into v_reserved from public.billing_usage u
   where u.user_id = p_user_id and u.kind = 'free' and u.state = 'reserved';

  return jsonb_build_object('free_total', v_total, 'free_used_seed', v_seed,
                            'free_settled', v_settled, 'free_reserved', v_reserved);
end;
$$;

-- Atomically: verify ownership, require `awaiting_upload`, check the free allowance
-- (seeded + settled + reserved, so pending reservations count) and reserve + queue.
-- A retried /start finds the existing ledger row and never charges twice.
create or replace function public.billing_admit(p_user_id uuid, p_swing_id uuid, p_kind text,
                                                p_product_id text, p_size_bytes bigint,
                                                p_free_total integer)
returns jsonb
language plpgsql
security definer
set search_path = ''
as $$
declare
  v_swing     public.swings;
  v_existing  public.billing_usage;
  v_grant     integer := greatest(coalesce(p_free_total, 0), 1);
  v_total     integer;
  v_seed      integer;
  v_accounted integer;
begin
  if p_kind not in ('free', 'paid') then
    raise exception 'unknown billing kind';
  end if;

  select * into v_swing from public.swings s where s.id = p_swing_id for update;
  if not found then
    return jsonb_build_object('status', 'not_found');
  end if;
  if v_swing.user_id <> p_user_id then
    return jsonb_build_object('status', 'not_found');
  end if;
  if v_swing.status <> 'awaiting_upload' then
    return jsonb_build_object('status', 'not_awaiting', 'swing_status', v_swing.status);
  end if;

  insert into public.billing_accounts (user_id, free_total, free_used_seed, grandfather_seeded,
                                       created_at, updated_at)
  values (
    p_user_id,
    v_grant,
    least(v_grant, coalesce((select count(*) from public.swings s
                              where s.user_id = p_user_id and s.status = 'complete'), 0)),
    true, now(), now())
  on conflict (user_id) do update
    set free_total = excluded.free_total, updated_at = now();

  select a.free_total, a.free_used_seed into v_total, v_seed
    from public.billing_accounts a where a.user_id = p_user_id;

  select * into v_existing from public.billing_usage u where u.swing_id = p_swing_id;
  if found then
    return jsonb_build_object('status', 'already', 'kind', v_existing.kind, 'state', v_existing.state);
  end if;

  if p_kind = 'free' then
    select v_seed
         + (select count(*) from public.billing_usage u
             where u.user_id = p_user_id and u.kind = 'free' and u.state = 'settled')
         + (select count(*) from public.billing_usage u
             where u.user_id = p_user_id and u.kind = 'free' and u.state = 'reserved')
      into v_accounted;
    if v_accounted >= v_total then
      return jsonb_build_object('status', 'exhausted', 'free_total', v_total, 'free_accounted', v_accounted);
    end if;
  end if;

  insert into public.billing_usage (swing_id, user_id, kind, state, product_id, created_at, updated_at)
  values (p_swing_id, p_user_id, p_kind, 'reserved', p_product_id, now(), now());

  update public.swings s
     set status = 'queued', size_bytes = coalesce(p_size_bytes, s.size_bytes), updated_at = now()
   where s.id = p_swing_id;

  return jsonb_build_object('status', 'admitted', 'kind', p_kind);
end;
$$;

-- Terminal settlement: the job state and the ledger move in one transaction, so a
-- worker crash can never strand or duplicate an analysis. Two guards:
--   * the row must still be `processing`, so a deleted or already-terminal job is
--     never resurrected; and
--   * `p_attempts` must match, so a slow worker from an earlier attempt cannot
--     settle (or release) the credit of the attempt that owns the job now.
--   * `p_expected_updated_at` (used by the janitor) pins the heartbeat the
--     caller read, so a heartbeat that lands between that read and this write
--     wins and the credit is not touched.
--   * `p_older_than` (also the janitor's) requires the row to still be older
--     than the staleness cutoff at write time. Timestamps are second-resolution,
--     so an exact-match check alone would miss a heartbeat inside the same second.
-- The ledger only moves when this call actually performed the guarded transition:
-- a deleted swing keeps its analysis consumed, exactly as the product requires.
create or replace function public.billing_finish(p_swing_id uuid, p_status text, p_attempts integer,
                                                 p_expected_updated_at timestamptz default null,
                                                 p_older_than timestamptz default null,
                                                 p_error_code text default null,
                                                 p_notice text default null, p_progress jsonb default null,
                                                 p_summary jsonb default null, p_analysis_version text default null,
                                                 p_thumb_key text default null, p_result_key text default null,
                                                 p_video_key text default null)
returns setof public.swings
language plpgsql
security definer
set search_path = ''
as $$
declare
  v_row public.swings;
begin
  if p_status not in ('complete', 'failed') then
    raise exception 'unknown billing settlement status';
  end if;

  update public.swings s
     set status             = p_status,
         error_code         = case when p_status = 'failed' then p_error_code else null end,
         notice             = coalesce(p_notice, s.notice),
         progress           = coalesce(p_progress, s.progress),
         summary            = coalesce(p_summary, s.summary),
         analysis_version   = coalesce(p_analysis_version, s.analysis_version),
         thumb_key          = coalesce(p_thumb_key, s.thumb_key),
         result_key         = coalesce(p_result_key, s.result_key),
         video_key          = coalesce(p_video_key, s.video_key),
         finished_at        = now(),
         updated_at         = now()
   where s.id = p_swing_id
     and s.status = 'processing'
     and s.attempts = p_attempts
     and (p_expected_updated_at is null or s.updated_at = p_expected_updated_at)
     and (p_older_than is null or s.updated_at < p_older_than)
   returning s.* into v_row;

  if v_row.id is null then
    return;
  end if;

  -- Idempotent: only a still-reserved row changes, and only once.
  update public.billing_usage u
     set state = case when p_status = 'complete' then 'settled' else 'released' end,
         settled_at = now(),
         updated_at = now()
   where u.swing_id = p_swing_id and u.state = 'reserved';

  return next v_row;
end;
$$;

revoke all on function public.billing_status(uuid, integer) from public, anon, authenticated;
revoke all on function public.billing_admit(uuid, uuid, text, text, bigint, integer) from public, anon, authenticated;
revoke all on function public.billing_finish(uuid, text, integer, timestamptz, timestamptz, text, text, jsonb, jsonb, text, text, text, text)
  from public, anon, authenticated;

grant execute on function public.billing_status(uuid, integer) to service_role;
grant execute on function public.billing_admit(uuid, uuid, text, text, bigint, integer) to service_role;
grant execute on function public.billing_finish(uuid, text, integer, timestamptz, timestamptz, text, text, jsonb, jsonb, text, text, text, text)
  to service_role;
