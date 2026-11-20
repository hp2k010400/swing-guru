-- Swing Guru V1 schema: one row per swing, which is also its analysis job.
-- Apply with the Supabase CLI (`supabase db push`) or paste into the SQL editor.

create table if not exists public.swings (
  id                uuid primary key,
  user_id           uuid not null references auth.users (id) on delete cascade,
  status            text not null check (status in ('awaiting_upload', 'queued', 'processing', 'complete', 'failed')),
  progress          jsonb,
  error_code        text,
  notice            text,
  club              text check (club in ('Driver', '3 Wood', '5 Wood', 'Hybrid', 'Irons', 'Wedges', 'Other')),
  file_name         text,
  content_type      text,
  size_bytes        bigint,
  captured_at       timestamptz,
  created_at        timestamptz not null default now(),
  updated_at        timestamptz not null default now(),
  started_at        timestamptz,
  finished_at       timestamptz,
  attempts          integer not null default 0,
  source_key        text,
  result_key        text,
  video_key         text,
  thumb_key         text,
  summary           jsonb,
  analysis_version  text,
  source_deleted_at timestamptz
);

create index if not exists swings_user_created on public.swings (user_id, created_at desc);
create index if not exists swings_queue on public.swings (status, created_at) where status in ('queued', 'processing');

-- The mobile app never talks to this table directly: every read and write goes
-- through the analysis service, which uses the service role. RLS is still on,
-- with owner-only read as defence in depth, and no client write policies at all.
alter table public.swings enable row level security;

drop policy if exists "owners can read their swings" on public.swings;
create policy "owners can read their swings" on public.swings
  for select to authenticated using ((select auth.uid()) = user_id);

-- Atomically claim the next queued job (or one whose worker stopped heartbeating).
-- FOR UPDATE SKIP LOCKED makes this safe with several workers.
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
      order by q.created_at
      for update skip locked
      limit 1)
  returning s.*;
$$;

revoke all on function public.claim_swing_job(timestamptz, integer) from public, anon, authenticated;
grant execute on function public.claim_swing_job(timestamptz, integer) to service_role;

-- Private bucket for originals, analysis JSON, replay video and thumbnails.
-- Objects are only reachable through short-lived signed URLs the service creates.
insert into storage.buckets (id, name, public, file_size_limit, allowed_mime_types)
values ('swings', 'swings', false, 524288000,
        array['video/mp4', 'video/quicktime', 'video/x-m4v', 'application/json', 'image/jpeg'])
on conflict (id) do update
  set public = false, file_size_limit = excluded.file_size_limit, allowed_mime_types = excluded.allowed_mime_types;
