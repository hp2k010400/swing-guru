-- Swing Guru billing: the free analysis allowance becomes three per UTC calendar month.
--
-- Additive upgrade of 20261002000000_billing.sql (which is never edited). Rollout notes are in
-- docs/agent-work/monthly-allowance/IMPLEMENTATION.md and .../ROLLOUT.md.
--
-- Model
-- * A free analysis is charged to the month it COMPLETES in (UTC). `charge_month` is that month's
--   first day; `settled_at` keeps the exact completion instant.
-- * Before admission, the current month's charged rows (settled + forfeited) plus EVERY outstanding
--   free reservation (whatever month it was admitted in) must be below the allowance. A reservation
--   admitted on the last day of a month therefore keeps holding a slot into the next month until it
--   completes (charged to the new month) or fails (released once).
-- * Voluntarily deleting queued/processing free work forfeits its slot in the deletion month
--   (`state = 'forfeited'`), so a late worker failure cannot return it and it does not reserve
--   forever. Deleting a completed swing never returns an analysis.
-- * `billing_accounts.free_used_seed` (the lifetime grandfather counter) is no longer read: pre-ledger
--   completed swings are grandfathered, and the rollout grants the current month's credits minus any
--   free ledger entries already accounted for this month. The old column is left in place (additive).
--
-- Locking discipline (every ledger function shares it, so concurrent admissions and settlements
-- cannot interleave a stale count with a fresh one):
--   * a function that knows the job locks that `public.swings` row first (`for update`), then the
--     user's `billing_accounts` row, then touches `billing_usage`. The swing row alone serialises
--     only retries of ONE job - different jobs must serialise on the account row.
--   * `billing_status` is account-only: it locks the account row and reads every count in a single
--     aggregate statement.
--   * counts are always one statement, so `settled`/`forfeited`/`reserved` come from one snapshot.
--   * in Postgres `now()` is the transaction timestamp, so every statement in one call sees the same
--     instant; the SQLite mirror reads its injected clock once, after `BEGIN IMMEDIATE`, to match.
--
-- Historical data: no table, column or function is dropped, and `public.swings` is untouched. The
-- migration DOES rewrite `billing_usage` rows once: it stamps `admitted_month`/`charge_month` on
-- existing rows (settled rows onto their completion month) and forfeits legacy orphan reservations
-- into their admission month. `billing_accounts.free_used_seed` is preserved unchanged - it is no
-- longer read, but nothing resets it.

alter table public.billing_usage add column if not exists charge_month date;
alter table public.billing_usage add column if not exists admitted_month date;

-- `forfeited` joins the terminal states. The inline check from the previous migration is
-- auto-named, so drop it by definition rather than by a guessed name (idempotent re-run).
do $$
declare
  constraint_name text;
begin
  for constraint_name in
    select con.conname
      from pg_constraint con
     where con.conrelid = 'public.billing_usage'::regclass
       and con.contype = 'c'
       and pg_get_constraintdef(con.oid) ilike '%''reserved''%'
  loop
    execute format('alter table public.billing_usage drop constraint %I', constraint_name);
  end loop;
end;
$$;

alter table public.billing_usage
  add constraint billing_usage_state_check
  check (state in ('reserved', 'settled', 'released', 'forfeited'));

create index if not exists billing_usage_month on public.billing_usage (user_id, kind, state, charge_month);
create index if not exists billing_usage_admitted on public.billing_usage (user_id, kind, admitted_month);

-- Backfill: every existing row keeps the month it was admitted in (for legacy reservation
-- attribution); settled rows are attributed to their completion month, preferring the swing's
-- finished_at, then the ledger's settled_at, then created_at.
update public.billing_usage u
   set admitted_month = date_trunc('month', u.created_at at time zone 'UTC')::date
 where u.admitted_month is null;

update public.billing_usage u
   set charge_month = date_trunc(
         'month',
         coalesce((select s.finished_at from public.swings s where s.id = u.swing_id), u.settled_at, u.created_at)
         at time zone 'UTC')::date
 where u.state = 'settled'
   and u.charge_month is null;

-- Legacy orphan reservations (their swing is gone, or already terminal) are forfeited into their
-- recorded admission month: they must not reserve a slot forever across future months. A job that
-- is genuinely still in flight (queued/processing) keeps its reservation.
update public.billing_usage u
   set state       = 'forfeited',
       charge_month = coalesce(u.admitted_month, date_trunc('month', u.created_at at time zone 'UTC')::date),
       settled_at  = coalesce(u.settled_at, u.created_at),
       updated_at  = now()
 where u.kind = 'free'
   and u.state = 'reserved'
   and not exists (select 1 from public.swings s
                    where s.id = u.swing_id and s.status in ('queued', 'processing'));

-- ---------------------------------------------------------------------------------------------
-- RPCs. Same signatures as the previous migration, replaced atomically (`create or replace`), so
-- no stale callable interface is left behind for production to bypass the monthly logic.
-- ---------------------------------------------------------------------------------------------

create or replace function public.billing_status(p_user_id uuid, p_free_total integer)
returns jsonb
language plpgsql
security definer
set search_path = ''
as $$
declare
  v_grant     integer := greatest(coalesce(p_free_total, 0), 1);
  v_total     integer;
  v_month     date := date_trunc('month', now() at time zone 'UTC')::date;
  v_settled   integer;
  v_forfeited integer;
  v_reserved  integer;
begin
  -- The account row is created once; free_used_seed is deliberately not read or written any more.
  -- The upsert takes the account row lock, which is what serialises this read against every
  -- admission, settlement and deletion for the same user.
  insert into public.billing_accounts (user_id, free_total, free_used_seed, grandfather_seeded,
                                       created_at, updated_at)
  values (p_user_id, v_grant, 0, true, now(), now())
  on conflict (user_id) do update
    set free_total = excluded.free_total, updated_at = now();

  select a.free_total into v_total from public.billing_accounts a where a.user_id = p_user_id;

  -- One statement, one snapshot: settled + forfeited for this month and outstanding reservations
  -- together. Two separate reads under READ COMMITTED could see a reservation that another
  -- transaction is just settling as neither charged nor reserved.
  select count(*) filter (where u.state = 'settled' and u.charge_month = v_month),
         count(*) filter (where u.state = 'forfeited' and u.charge_month = v_month),
         count(*) filter (where u.state = 'reserved')
    into v_settled, v_forfeited, v_reserved
    from public.billing_usage u
   where u.user_id = p_user_id and u.kind = 'free';

  return jsonb_build_object(
    'free_total', v_total,
    'free_settled', v_settled,
    'free_forfeited', v_forfeited,
    'free_reserved', v_reserved,
    'allowance_period_start', v_month::timestamp at time zone 'UTC',
    'allowance_resets_at', (v_month + interval '1 month')::timestamp at time zone 'UTC',
    'allowance_timezone', 'UTC',
    'allowance_model', 'calendar_month');
end;
$$;

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
  v_month     date := date_trunc('month', now() at time zone 'UTC')::date;
  v_settled   integer;
  v_forfeited integer;
  v_reserved  integer;
  v_used      integer;
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
  values (p_user_id, v_grant, 0, true, now(), now())
  on conflict (user_id) do update
    set free_total = excluded.free_total, updated_at = now();

  select a.free_total into v_total from public.billing_accounts a where a.user_id = p_user_id;

  -- Idempotence comes before the allowance check: a retried /start for a job that already has a
  -- ledger row is never a 402/409, even at the last credit.
  select * into v_existing from public.billing_usage u where u.swing_id = p_swing_id;
  if found then
    return jsonb_build_object('status', 'already', 'kind', v_existing.kind, 'state', v_existing.state);
  end if;

  if p_kind = 'free' then
    -- One statement, one snapshot (see billing_status). The account row is already locked above, so
    -- no concurrent settlement or deletion can land between this read and the insert below.
    select count(*) filter (where u.state = 'settled' and u.charge_month = v_month),
           count(*) filter (where u.state = 'forfeited' and u.charge_month = v_month),
           count(*) filter (where u.state = 'reserved')
      into v_settled, v_forfeited, v_reserved
      from public.billing_usage u
     where u.user_id = p_user_id and u.kind = 'free';
    v_used := v_settled + v_forfeited;

    if v_used >= v_total then
      return jsonb_build_object('status', 'exhausted', 'free_total', v_total,
                                'free_used', v_used, 'free_reserved', v_reserved);
    end if;
    -- Charged < total but every remaining slot is held by a job that has not finished yet: this is
    -- a wait state, not a paywall.
    if v_used + v_reserved >= v_total then
      return jsonb_build_object('status', 'in_progress', 'free_total', v_total,
                                'free_used', v_used, 'free_reserved', v_reserved);
    end if;
  end if;

  insert into public.billing_usage (swing_id, user_id, kind, state, product_id,
                                    admitted_month, created_at, updated_at)
  values (p_swing_id, p_user_id, p_kind, 'reserved', p_product_id, v_month, now(), now());

  update public.swings s
     set status = 'queued', size_bytes = coalesce(p_size_bytes, s.size_bytes), updated_at = now()
   where s.id = p_swing_id;

  return jsonb_build_object('status', 'admitted', 'kind', p_kind);
end;
$$;

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

  -- Lock order: the job row first, then the user's ledger account. The account lock is taken BEFORE
  -- the guarded status write so a concurrent admission cannot read a count that does not yet include
  -- this settlement; the swing lock alone would only serialise retries of this one job.
  select * into v_row from public.swings s where s.id = p_swing_id for update;
  if not found then
    return;
  end if;

  insert into public.billing_accounts (user_id, free_used_seed, grandfather_seeded, created_at, updated_at)
  values (v_row.user_id, 0, true, now(), now())
  on conflict (user_id) do update set updated_at = now();

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

  if not found then
    return;
  end if;

  -- Idempotent: only a still-reserved row changes, and only once. A completion charges the month
  -- it actually completed in; a failure releases the slot without charging anything.
  -- The completion month and the settlement instant are two views of ONE `now()`.
  update public.billing_usage u
     set state        = case when p_status = 'complete' then 'settled' else 'released' end,
         charge_month = case when p_status = 'complete'
                             then date_trunc('month', now() at time zone 'UTC')::date
                             else null end,
         settled_at   = now(),
         updated_at   = now()
   where u.swing_id = p_swing_id and u.state = 'reserved';

  return next v_row;
end;
$$;

-- Voluntary deletion of a swing: ownership-checked, and a still-reserved free slot is forfeited in
-- the deletion month so it neither blocks a future month forever nor returns to the golfer later.
-- A completed (settled) row is untouched: deleted analyses stay consumed.
create or replace function public.billing_delete(p_user_id uuid, p_swing_id uuid)
returns jsonb
language plpgsql
security definer
set search_path = ''
as $$
declare
  v_row       public.swings;
  v_month     date := date_trunc('month', now() at time zone 'UTC')::date;
  v_forfeited integer := 0;
begin
  select * into v_row from public.swings s
   where s.id = p_swing_id and s.user_id = p_user_id for update;
  if not found then
    return jsonb_build_object('status', 'not_found');
  end if;

  -- Same lock order as admission and settlement: the job row, then the account row, then the ledger.
  insert into public.billing_accounts (user_id, free_used_seed, grandfather_seeded, created_at, updated_at)
  values (v_row.user_id, 0, true, now(), now())
  on conflict (user_id) do update set updated_at = now();

  update public.billing_usage u
     set state        = 'forfeited',
         charge_month = v_month,
         settled_at   = now(),
         updated_at   = now()
   where u.swing_id = p_swing_id and u.kind = 'free' and u.state = 'reserved';
  get diagnostics v_forfeited = row_count;

  delete from public.swings s where s.id = p_swing_id and s.user_id = p_user_id;

  return jsonb_build_object('status', 'deleted', 'forfeited', v_forfeited, 'swing', to_jsonb(v_row));
end;
$$;

revoke all on function public.billing_status(uuid, integer) from public, anon, authenticated;
revoke all on function public.billing_admit(uuid, uuid, text, text, bigint, integer) from public, anon, authenticated;
revoke all on function public.billing_finish(uuid, text, integer, timestamptz, timestamptz, text, text, jsonb, jsonb, text, text, text, text)
  from public, anon, authenticated;
revoke all on function public.billing_delete(uuid, uuid) from public, anon, authenticated;

grant execute on function public.billing_status(uuid, integer) to service_role;
grant execute on function public.billing_admit(uuid, uuid, text, text, bigint, integer) to service_role;
grant execute on function public.billing_finish(uuid, text, integer, timestamptz, timestamptz, text, text, jsonb, jsonb, text, text, text, text)
  to service_role;
grant execute on function public.billing_delete(uuid, uuid) to service_role;
