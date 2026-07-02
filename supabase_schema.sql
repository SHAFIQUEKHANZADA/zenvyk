-- Zenvyk Guardian — plan-enforcement schema.
-- Run this in the Supabase SQL editor. The backend reads/writes these tables
-- with the SERVICE_ROLE key (server-side only), so RLS is not required for it,
-- but enable RLS + policies if the dashboard reads these client-side.

-- One row per user; holds the plan. `id` == auth.users.id.
create table if not exists public.profiles (
    id   uuid primary key references auth.users (id) on delete cascade,
    plan text not null default 'free'
);

-- API keys. The backend looks a key up here to find its owning user.
-- (For production, store a hash instead of the raw key and look up by hash.)
create table if not exists public.api_keys (
    id         uuid primary key default gen_random_uuid(),
    user_id    uuid not null references auth.users (id) on delete cascade,
    key        text not null unique,
    revoked    boolean not null default false,
    created_at timestamptz not null default now()
);
create index if not exists api_keys_key_idx on public.api_keys (key);

-- Per-user, per-calendar-month request counter.
create table if not exists public.usage (
    user_id uuid not null references auth.users (id) on delete cascade,
    month   text not null,               -- 'YYYY-MM' (UTC)
    count   integer not null default 0,
    primary key (user_id, month)
);

-- Atomic increment used on each successful /v1/verify.
create or replace function public.increment_usage(p_user_id uuid, p_month text)
returns integer
language plpgsql
as $$
declare
    new_count integer;
begin
    insert into public.usage (user_id, month, count)
    values (p_user_id, p_month, 1)
    on conflict (user_id, month)
    do update set count = public.usage.count + 1
    returning count into new_count;
    return new_count;
end;
$$;
