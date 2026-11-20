-- Swing Guru release accounts: durable Apple grants and a deletable account.
--
-- Additive. The previous migrations are never edited. Setup and rollout notes live in
-- docs/agent-work/release-accounts/{CONFIGURATION,IMPLEMENTATION}.md.
--
-- Why a *grant* table rather than one credential per user:
-- * Every authorization-code exchange mints its own refresh token, and Apple does not guarantee that
--   a new authorization invalidates the previous one. Keeping a single row per user meant a newer
--   token could overwrite an older one (losing a live grant) or inherit an account-level
--   "revoked" flag it never earned. `public.apple_grants` is therefore append-only: one row per
--   minted token, each with its own `state` and its own confirmed revocation timestamp.
-- * A revocation is recorded per grant, by the attempt that got Apple's HTTP 200, through a
--   compare-and-set. A grant row may only be deleted once *that row* is revoked.
-- * The account-level `account_deletions.apple_revocation` is a derived summary for the API
--   response, never the proof that a particular token is dead.
--
-- `public.apple_grants` deliberately has NO foreign key to `auth.users`. A registration exchange
-- that races a deletion can store a minted token after the auth row is gone; a cascading delete
-- would destroy the only record of a live grant. The guarded delete is the only path that removes a
-- row.
--
-- Lock order (every cooperating write uses the same one, so no two of them can deadlock):
--   1. the per-user lifecycle advisory lock (`pg_advisory_xact_lock(hashtextextended(user_id, 0))`)
--   2. the user's `public.swings` rows (`for update`)
--   3. the user's `public.billing_accounts` row, then `public.billing_usage`
-- `account_wipe` takes all three; `billing_finish`/`billing_admit`/`billing_delete` take 1-3 in that
-- order too; the swing/queue/finish helpers take 1-2.

-- A never-released earlier revision of this file used `public.apple_credentials`; drop it if some
-- local environment ran that revision. Nothing in the released schema references it.
drop table if exists public.apple_credentials;

create table if not exists public.apple_grants (
  id                       uuid primary key,
  user_id                  uuid not null,
  apple_subject            text not null,
  client_id                text not null,
  refresh_token_ciphertext text not null,
  key_version              integer not null default 1,
  state                    text not null default 'active' check (state in ('active', 'revoked')),
  revoked_at               timestamptz,
  created_at               timestamptz not null default now(),
  updated_at               timestamptz not null default now()
);

create index if not exists apple_grants_user_state on public.apple_grants (user_id, state);

create table if not exists public.account_deletions (
  user_id            uuid primary key,
  status             text not null check (status in ('deleting', 'deleted')),
  storage_cleared_at timestamptz,
  data_cleared_at    timestamptz,
  -- Derived summary for the API response: 'revoked' (every grant this account ever stored is
  -- confirmed revoked), 'not_required' (no grant was ever stored), 'manual_required' (TN3194
  -- fallback: at least one grant exists that this server could not revoke). NULL while pending.
  apple_revocation   text check (apple_revocation in ('revoked', 'not_required', 'manual_required')),
  auth_deleted_at    timestamptz,
  -- Late/racing uploads: an upload URL issued before the sweep can still be used after it, and a
  -- worker can crash after writing objects. The janitor keeps re-clearing this owner's prefix until
  -- `cleanup_until`, then records `cleanup_done_at`.
  cleanup_until      timestamptz,
  cleanup_done_at    timestamptz,
  attempts           integer not null default 0,
  last_error_code    text,
  created_at         timestamptz not null default now(),
  updated_at         timestamptz not null default now()
);

create index if not exists account_deletions_status on public.account_deletions (status);
create index if not exists account_deletions_reconcile
  on public.account_deletions (status, updated_at, cleanup_until);

alter table public.apple_grants enable row level security;
alter table public.account_deletions enable row level security;

revoke all on table public.apple_grants from public, anon, authenticated;
revoke all on table public.account_deletions from public, anon, authenticated;

-- ---------------------------------------------------------------------------------------------
-- Access guard + lifecycle
-- ---------------------------------------------------------------------------------------------

create or replace function public.account_status(p_user_id uuid)
returns text
language plpgsql
security definer
set search_path = ''
as $$
declare
  v_status text;
begin
  select d.status into v_status from public.account_deletions d where d.user_id = p_user_id;
  return coalesce(v_status, 'active');
end;
$$;

create or replace function public.account_deletion_begin(p_user_id uuid)
returns jsonb
language plpgsql
security definer
set search_path = ''
as $$
declare
  v_row public.account_deletions;
begin
  perform pg_advisory_xact_lock(hashtextextended(p_user_id::text, 0));

  insert into public.account_deletions (user_id, status, attempts, created_at, updated_at)
  values (p_user_id, 'deleting', 0, now(), now())
  on conflict (user_id) do nothing;

  update public.account_deletions d
     set attempts = d.attempts + 1, updated_at = now()
   where d.user_id = p_user_id;

  select * into v_row from public.account_deletions d where d.user_id = p_user_id;
  return to_jsonb(v_row);
end;
$$;

create or replace function public.account_deletion_get(p_user_id uuid)
returns jsonb
language sql
security definer
set search_path = ''
as $$
  select to_jsonb(d) from public.account_deletions d where d.user_id = p_user_id;
$$;

create or replace function public.account_deletion_update(p_user_id uuid, p_apple_revoked boolean,
                                                          p_storage_cleared boolean, p_data_cleared boolean,
                                                          p_auth_deleted boolean, p_apple_revocation text,
                                                          p_error_code text,
                                                          p_cleanup_until timestamptz default null,
                                                          p_cleanup_done boolean default false)
returns jsonb
language plpgsql
security definer
set search_path = ''
as $$
declare
  v_row public.account_deletions;
begin
  perform pg_advisory_xact_lock(hashtextextended(p_user_id::text, 0));

  update public.account_deletions d
     set storage_cleared_at = case when p_storage_cleared then coalesce(d.storage_cleared_at, now())
                                   else d.storage_cleared_at end,
         data_cleared_at    = case when p_data_cleared then coalesce(d.data_cleared_at, now())
                                   else d.data_cleared_at end,
         auth_deleted_at    = case when p_auth_deleted then coalesce(d.auth_deleted_at, now())
                                   else d.auth_deleted_at end,
         -- The summary is monotonic for 'revoked' only: a later pass may upgrade
         -- manual_required/not_required once a grant is confirmed revoked, but a confirmed summary
         -- can never be downgraded to a guess.
         apple_revocation   = case
                                when d.apple_revocation = 'revoked' then 'revoked'
                                when p_apple_revocation is null then d.apple_revocation
                                else p_apple_revocation
                              end,
         cleanup_until      = coalesce(d.cleanup_until, p_cleanup_until),
         cleanup_done_at    = case when p_cleanup_done then coalesce(d.cleanup_done_at, now())
                                   else d.cleanup_done_at end,
         last_error_code    = p_error_code,
         updated_at         = now()
   where d.user_id = p_user_id;

  select * into v_row from public.account_deletions d where d.user_id = p_user_id;
  return coalesce(to_jsonb(v_row), '{}'::jsonb);
end;
$$;

-- Completion is refused while any grant for this account is still active. That check is what makes
-- "we deleted an unrevoked newer token" impossible, not the account-level summary.
create or replace function public.account_deletion_finish(p_user_id uuid)
returns jsonb
language plpgsql
security definer
set search_path = ''
as $$
declare
  v_row    public.account_deletions;
  v_active integer;
begin
  perform pg_advisory_xact_lock(hashtextextended(p_user_id::text, 0));

  select * into v_row from public.account_deletions d where d.user_id = p_user_id for update;
  if not found then
    return '{}'::jsonb;
  end if;

  select count(*) into v_active from public.apple_grants g
   where g.user_id = p_user_id and g.state <> 'revoked';
  if v_row.apple_revocation is null or v_row.storage_cleared_at is null
     or v_row.data_cleared_at is null then
    return jsonb_build_object('error', 'prerequisites_missing', 'user_id', p_user_id);
  end if;
  if v_active > 0 then
    return jsonb_build_object('error', 'unrevoked_grant', 'user_id', p_user_id, 'active', v_active);
  end if;

  update public.account_deletions d
     set status = 'deleted', last_error_code = null, updated_at = now()
   where d.user_id = p_user_id;

  select * into v_row from public.account_deletions d where d.user_id = p_user_id;
  return to_jsonb(v_row);
end;
$$;

-- The janitor's work list: half-finished deletions, accounts whose late-upload window is still open,
-- and *any* account (even one already marked deleted) that still holds an unrevoked grant - a grant
-- can be stored by an exchange that raced the deletion.
create or replace function public.account_deletions_for_reconcile(p_stuck_before timestamptz,
                                                                  p_cleanup_before timestamptz)
returns setof public.account_deletions
language sql
security definer
set search_path = ''
as $$
  select * from public.account_deletions d
   where (d.status = 'deleting' and d.updated_at < p_stuck_before)
      or (d.status = 'deleted' and d.cleanup_done_at is null
          and d.cleanup_until is not null and d.cleanup_until <= p_cleanup_before)
      or exists (select 1 from public.apple_grants g
                  where g.user_id = d.user_id and g.state <> 'revoked');
$$;

-- ---------------------------------------------------------------------------------------------
-- Apple grants: append-only, per-grant revocation
-- ---------------------------------------------------------------------------------------------

-- Registration for a live account. Refused (nothing stored) once a deletion owns the account; the
-- caller then quarantines the minted token through the deletion variant below.
create or replace function public.apple_grant_put(p_user_id uuid, p_grant_id uuid, p_subject text,
                                                  p_client_id text, p_token text)
returns jsonb
language plpgsql
security definer
set search_path = ''
as $$
begin
  perform pg_advisory_xact_lock(hashtextextended(p_user_id::text, 0));

  if exists (select 1 from public.account_deletions d where d.user_id = p_user_id) then
    return jsonb_build_object('stored', false);
  end if;

  insert into public.apple_grants (id, user_id, apple_subject, client_id, refresh_token_ciphertext,
                                   key_version, state, created_at, updated_at)
  values (p_grant_id, p_user_id, p_subject, p_client_id, p_token, 1, 'active', now(), now())
  on conflict (id) do nothing;

  return jsonb_build_object('stored', true, 'grant_id', p_grant_id);
end;
$$;

-- The deletion-owned variant: allowed whenever a tombstone exists, and it *appends* a new grant
-- rather than replacing anything, so two overlapping exchanges can never overwrite each other's
-- token. It never touches the account summary.
create or replace function public.apple_grant_put_for_deletion(p_user_id uuid, p_grant_id uuid,
                                                               p_subject text, p_client_id text,
                                                               p_token text)
returns jsonb
language plpgsql
security definer
set search_path = ''
as $$
begin
  perform pg_advisory_xact_lock(hashtextextended(p_user_id::text, 0));

  if not exists (select 1 from public.account_deletions d where d.user_id = p_user_id) then
    return jsonb_build_object('stored', false);
  end if;

  insert into public.apple_grants (id, user_id, apple_subject, client_id, refresh_token_ciphertext,
                                   key_version, state, created_at, updated_at)
  values (p_grant_id, p_user_id, p_subject, p_client_id, p_token, 1, 'active', now(), now())
  on conflict (id) do nothing;

  return jsonb_build_object('stored', true, 'grant_id', p_grant_id);
end;
$$;

create or replace function public.apple_grants_unrevoked(p_user_id uuid)
returns jsonb
language sql
security definer
set search_path = ''
as $$
  select coalesce(jsonb_agg(to_jsonb(g) order by g.created_at), '[]'::jsonb)
    from public.apple_grants g
   where g.user_id = p_user_id and g.state <> 'revoked';
$$;

create or replace function public.apple_grant_counts(p_user_id uuid)
returns jsonb
language sql
security definer
set search_path = ''
as $$
  select jsonb_build_object(
    'active', count(*) filter (where g.state <> 'revoked'),
    'revoked', count(*) filter (where g.state = 'revoked'))
    from public.apple_grants g
   where g.user_id = p_user_id;
$$;

-- Per-grant compare-and-set: only the first attempt to confirm a revocation writes it, and a
-- revoked grant can never be flipped back to active. Returns the row, or {} when it was already
-- revoked (an idempotent no-op, not an error).
create or replace function public.apple_grant_mark_revoked(p_grant_id uuid, p_user_id uuid)
returns jsonb
language plpgsql
security definer
set search_path = ''
as $$
declare
  v_row public.apple_grants;
begin
  perform pg_advisory_xact_lock(hashtextextended(p_user_id::text, 0));

  update public.apple_grants g
     set state = 'revoked', revoked_at = now(), updated_at = now()
   where g.id = p_grant_id and g.user_id = p_user_id and g.state = 'active'
   returning g.* into v_row;

  if not found then
    return '{}'::jsonb;
  end if;
  return to_jsonb(v_row);
end;
$$;

-- Only revoked rows leave the table. There is deliberately no "delete this grant" that ignores the
-- state: a live token must never be dropped without a confirmed revocation.
create or replace function public.apple_grants_delete_revoked(p_user_id uuid)
returns jsonb
language plpgsql
security definer
set search_path = ''
as $$
declare
  v_deleted integer := 0;
  v_active  integer := 0;
begin
  perform pg_advisory_xact_lock(hashtextextended(p_user_id::text, 0));

  delete from public.apple_grants g where g.user_id = p_user_id and g.state = 'revoked';
  get diagnostics v_deleted = row_count;

  select count(*) into v_active from public.apple_grants g
   where g.user_id = p_user_id and g.state <> 'revoked';
  return jsonb_build_object('deleted', v_deleted, 'active', v_active);
end;
$$;

-- ---------------------------------------------------------------------------------------------
-- Deletion-aware swing/billing writes (lock order: advisory, swing rows, billing account)
-- ---------------------------------------------------------------------------------------------

create or replace function public.swing_create(p_user_id uuid, p_swing_id uuid, p_fields jsonb)
returns jsonb
language plpgsql
security definer
set search_path = ''
as $$
declare
  v_row public.swings;
begin
  perform pg_advisory_xact_lock(hashtextextended(p_user_id::text, 0));

  if exists (select 1 from public.account_deletions d where d.user_id = p_user_id) then
    return jsonb_build_object('blocked', true);
  end if;

  v_row := jsonb_populate_record(null::public.swings,
                                 p_fields || jsonb_build_object('id', p_swing_id, 'user_id', p_user_id));
  insert into public.swings values (v_row.*);

  return jsonb_build_object('id', p_swing_id);
end;
$$;

create or replace function public.swing_queue_if_awaiting(p_swing_id uuid, p_user_id uuid,
                                                          p_size_bytes bigint)
returns jsonb
language plpgsql
security definer
set search_path = ''
as $$
declare
  v_row public.swings;
begin
  perform pg_advisory_xact_lock(hashtextextended(p_user_id::text, 0));

  if exists (select 1 from public.account_deletions d where d.user_id = p_user_id) then
    return jsonb_build_object('blocked', true);
  end if;

  update public.swings s
     set status = 'queued', size_bytes = p_size_bytes, updated_at = now()
   where s.id = p_swing_id and s.user_id = p_user_id and s.status = 'awaiting_upload'
   returning s.* into v_row;

  if not found then
    return '{}'::jsonb;
  end if;
  return to_jsonb(v_row);
end;
$$;

create or replace function public.swing_finish(p_swing_id uuid, p_status text, p_attempts integer,
                                               p_expected_updated_at timestamptz default null,
                                               p_older_than timestamptz default null,
                                               p_error_code text default null, p_progress jsonb default null)
returns setof public.swings
language plpgsql
security definer
set search_path = ''
as $$
declare
  v_row public.swings;
begin
  if p_status not in ('complete', 'failed') then
    raise exception 'unknown terminal status';
  end if;

  update public.swings s
     set status = p_status,
         error_code = case when p_status = 'failed' then p_error_code else null end,
         progress = coalesce(p_progress, s.progress),
         finished_at = now(),
         updated_at = now()
   where s.id = p_swing_id
     and s.status = 'processing'
     and s.attempts = p_attempts
     and (p_expected_updated_at is null or s.updated_at = p_expected_updated_at)
     and (p_older_than is null or s.updated_at < p_older_than)
     and not exists (select 1 from public.account_deletions d where d.user_id = s.user_id)
   returning s.* into v_row;

  if v_row.id is null then
    return;
  end if;
  return next v_row;
end;
$$;

-- `billing_finish` keeps its swing-then-account order and gains both the account predicate and the
-- lifecycle lock, so it serialises with `account_wipe` instead of deadlocking against it.
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

  select * into v_row from public.swings s where s.id = p_swing_id for update;
  if not found then
    return;
  end if;
  perform pg_advisory_xact_lock(hashtextextended(v_row.user_id::text, 0));
  if exists (select 1 from public.account_deletions d where d.user_id = v_row.user_id) then
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
     and not exists (select 1 from public.account_deletions d where d.user_id = s.user_id)
   returning s.* into v_row;

  if not found then
    return;
  end if;

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

  perform pg_advisory_xact_lock(hashtextextended(p_user_id::text, 0));
  if exists (select 1 from public.account_deletions d where d.user_id = p_user_id) then
    return jsonb_build_object('status', 'blocked');
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

  select * into v_existing from public.billing_usage u where u.swing_id = p_swing_id;
  if found then
    return jsonb_build_object('status', 'already', 'kind', v_existing.kind, 'state', v_existing.state);
  end if;

  if p_kind = 'free' then
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

-- Voluntary swing deletion, with the same lock order and lifecycle lock as the other writers.
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

  perform pg_advisory_xact_lock(hashtextextended(v_row.user_id::text, 0));

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

create or replace function public.claim_swing_job(stale_before timestamptz, max_attempts integer)
returns setof public.swings
language sql
security definer
set search_path = ''
as $$
  update public.swings s
     set status = 'processing', attempts = s.attempts + 1, started_at = now(), updated_at = now()
   where s.id = (
     select q.id from public.swings q
      where (q.status = 'queued' or (q.status = 'processing' and q.updated_at < stale_before))
        and q.attempts < max_attempts
        and not exists (select 1 from public.account_deletions d where d.user_id = q.user_id)
      order by q.created_at
      for update skip locked
      limit 1)
   returning s.*;
$$;

-- Wipe order matches every other writer: lifecycle lock, then the user's swing rows, then the
-- billing account row, then the ledger, then the rows themselves.
create or replace function public.account_wipe(p_user_id uuid)
returns jsonb
language plpgsql
security definer
set search_path = ''
as $$
declare
  v_swings integer := 0;
  v_locked integer := 0;
begin
  perform pg_advisory_xact_lock(hashtextextended(p_user_id::text, 0));

  select count(*) into v_locked from (
    select 1 from public.swings s where s.user_id = p_user_id order by s.id for update
  ) locked_swings;

  if to_regclass('public.billing_accounts') is not null then
    insert into public.billing_accounts (user_id, free_used_seed, grandfather_seeded, created_at, updated_at)
    values (p_user_id, 0, true, now(), now())
    on conflict (user_id) do update set updated_at = now();
  end if;
  if to_regclass('public.billing_usage') is not null then
    delete from public.billing_usage u where u.user_id = p_user_id;
  end if;

  delete from public.swings s where s.user_id = p_user_id;
  get diagnostics v_swings = row_count;

  if to_regclass('public.billing_accounts') is not null then
    delete from public.billing_accounts a where a.user_id = p_user_id;
  end if;
  return jsonb_build_object('deleted', v_swings);
end;
$$;

revoke all on function public.account_status(uuid) from public, anon, authenticated;
revoke all on function public.account_deletion_begin(uuid) from public, anon, authenticated;
revoke all on function public.account_deletion_get(uuid) from public, anon, authenticated;
revoke all on function public.account_deletion_update(uuid, boolean, boolean, boolean, boolean, text, text, timestamptz, boolean)
  from public, anon, authenticated;
revoke all on function public.account_deletion_finish(uuid) from public, anon, authenticated;
revoke all on function public.account_deletions_for_reconcile(timestamptz, timestamptz)
  from public, anon, authenticated;
revoke all on function public.account_wipe(uuid) from public, anon, authenticated;
revoke all on function public.apple_grant_put(uuid, uuid, text, text, text) from public, anon, authenticated;
revoke all on function public.apple_grant_put_for_deletion(uuid, uuid, text, text, text)
  from public, anon, authenticated;
revoke all on function public.apple_grants_unrevoked(uuid) from public, anon, authenticated;
revoke all on function public.apple_grant_counts(uuid) from public, anon, authenticated;
revoke all on function public.apple_grant_mark_revoked(uuid, uuid) from public, anon, authenticated;
revoke all on function public.apple_grants_delete_revoked(uuid) from public, anon, authenticated;
revoke all on function public.swing_create(uuid, uuid, jsonb) from public, anon, authenticated;
revoke all on function public.swing_queue_if_awaiting(uuid, uuid, bigint) from public, anon, authenticated;
revoke all on function public.swing_finish(uuid, text, integer, timestamptz, timestamptz, text, jsonb)
  from public, anon, authenticated;
revoke all on function public.billing_admit(uuid, uuid, text, text, bigint, integer)
  from public, anon, authenticated;
revoke all on function public.billing_finish(uuid, text, integer, timestamptz, timestamptz, text, text, jsonb, jsonb, text, text, text, text)
  from public, anon, authenticated;
revoke all on function public.billing_delete(uuid, uuid) from public, anon, authenticated;
revoke all on function public.claim_swing_job(timestamptz, integer) from public, anon, authenticated;

grant execute on function public.account_status(uuid) to service_role;
grant execute on function public.account_deletion_begin(uuid) to service_role;
grant execute on function public.account_deletion_get(uuid) to service_role;
grant execute on function public.account_deletion_update(uuid, boolean, boolean, boolean, boolean, text, text, timestamptz, boolean)
  to service_role;
grant execute on function public.account_deletion_finish(uuid) to service_role;
grant execute on function public.account_deletions_for_reconcile(timestamptz, timestamptz) to service_role;
grant execute on function public.account_wipe(uuid) to service_role;
grant execute on function public.apple_grant_put(uuid, uuid, text, text, text) to service_role;
grant execute on function public.apple_grant_put_for_deletion(uuid, uuid, text, text, text) to service_role;
grant execute on function public.apple_grants_unrevoked(uuid) to service_role;
grant execute on function public.apple_grant_counts(uuid) to service_role;
grant execute on function public.apple_grant_mark_revoked(uuid, uuid) to service_role;
grant execute on function public.apple_grants_delete_revoked(uuid) to service_role;
grant execute on function public.swing_create(uuid, uuid, jsonb) to service_role;
grant execute on function public.swing_queue_if_awaiting(uuid, uuid, bigint) to service_role;
grant execute on function public.swing_finish(uuid, text, integer, timestamptz, timestamptz, text, jsonb)
  to service_role;
grant execute on function public.billing_admit(uuid, uuid, text, text, bigint, integer) to service_role;
grant execute on function public.billing_finish(uuid, text, integer, timestamptz, timestamptz, text, text, jsonb, jsonb, text, text, text, text)
  to service_role;
grant execute on function public.billing_delete(uuid, uuid) to service_role;
grant execute on function public.claim_swing_job(timestamptz, integer) to service_role;
