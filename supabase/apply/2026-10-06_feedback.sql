-- In-app feedback (Build 9). Two kinds:
--   analysis : "Was this analysis helpful?" thumbs up/down (+ optional comment) on one swing; one row
--              per user and swing (a second tap updates it);
--   general  : Profile -> Send feedback (category + message).
-- Service role only (the API writes through feedback_submit; clients have no table access).
-- Deleted with the account: user_id cascades from auth.users, which account deletion removes last.
-- A deleted swing keeps the feedback (swing_id set to null) until the account itself is deleted.
-- Safe to run twice.

create table if not exists public.feedback (
  id uuid primary key default gen_random_uuid(),
  user_id uuid not null references auth.users (id) on delete cascade,
  kind text not null check (kind in ('analysis', 'general')),
  swing_id uuid references public.swings (id) on delete set null,
  rating text check (rating in ('up', 'down')),
  category text check (category in ('swing_analysis', 'bug', 'feature', 'other')),
  message text check (message is null or char_length(message) <= 2000),
  context jsonb not null default '{}'::jsonb,
  app_version text check (app_version is null or char_length(app_version) <= 40),
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  check ((kind = 'analysis' and rating is not null) or (kind = 'general' and category is not null and message is not null))
);

create unique index if not exists feedback_one_per_swing on public.feedback (user_id, swing_id) where kind = 'analysis';
create index if not exists feedback_recent on public.feedback (created_at desc);

alter table public.feedback enable row level security;
revoke all on table public.feedback from public, anon, authenticated;
grant select, insert, update, delete on table public.feedback to service_role;

create or replace function public.feedback_submit(p_user_id uuid, p_kind text, p_swing_id uuid, p_rating text,
                                                  p_category text, p_message text, p_context jsonb,
                                                  p_app_version text)
returns jsonb
language plpgsql
security definer
set search_path = ''
as $$
declare
  v_id uuid;
begin
  -- An account in its deletion lifecycle takes no new writes (same rule as swing_create).
  if exists (select 1 from public.account_deletions d where d.user_id = p_user_id) then
    return jsonb_build_object('blocked', true);
  end if;
  if p_kind = 'analysis' then
    if p_swing_id is null or not exists (
        select 1 from public.swings s where s.id = p_swing_id and s.user_id = p_user_id) then
      return jsonb_build_object('not_found', true);
    end if;
    insert into public.feedback as f (user_id, kind, swing_id, rating, message, context, app_version)
    values (p_user_id, 'analysis', p_swing_id, p_rating, p_message, coalesce(p_context, '{}'::jsonb), p_app_version)
    on conflict (user_id, swing_id) where kind = 'analysis'
    do update set rating = excluded.rating, message = excluded.message, context = excluded.context,
                  app_version = excluded.app_version, updated_at = now()
    returning f.id into v_id;
  else
    insert into public.feedback (user_id, kind, category, message, context, app_version)
    values (p_user_id, 'general', p_category, p_message, coalesce(p_context, '{}'::jsonb), p_app_version)
    returning id into v_id;
  end if;
  return jsonb_build_object('id', v_id);
end;
$$;

revoke all on function public.feedback_submit(uuid, text, uuid, text, text, text, jsonb, text) from public, anon, authenticated;
grant execute on function public.feedback_submit(uuid, text, uuid, text, text, text, jsonb, text) to service_role;
