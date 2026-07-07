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

-- ===========================================================================
-- Playground chat history — one row per saved conversation (per user).
-- Read/written CLIENT-SIDE via the browser Supabase client, so RLS is REQUIRED
-- so each user only sees their own chats.
-- ===========================================================================
create table if not exists public.conversations (
    id         uuid primary key default gen_random_uuid(),
    user_id    uuid not null references auth.users (id) on delete cascade,
    title      text not null default 'New chat',
    messages   jsonb not null default '[]'::jsonb,
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now()
);
create index if not exists conversations_user_updated_idx
    on public.conversations (user_id, updated_at desc);

alter table public.conversations enable row level security;

-- A user may fully manage only their own conversations.
drop policy if exists "own conversations" on public.conversations;
create policy "own conversations" on public.conversations
    for all
    using (auth.uid() = user_id)
    with check (auth.uid() = user_id);

-- ===========================================================================
-- Guardian Resource Intelligence (GRI) — projects, phases, checkpoints, logs.
-- Server-side only (service-role). Enable RLS + policies if read client-side.
-- ===========================================================================

-- One analyzed project ("flight plan").
create table if not exists public.projects (
    id         uuid primary key default gen_random_uuid(),
    user_id    uuid references auth.users (id) on delete cascade,
    prompt     text,
    status     text not null default 'analyzed',   -- analyzed|running|done|queued
    meta       jsonb,                               -- best_provider, estimates, etc.
    created_at timestamptz not null default now()
);
create index if not exists projects_user_idx on public.projects (user_id);

-- The phases a project was split into.
create table if not exists public.project_phases (
    project_id uuid not null references public.projects (id) on delete cascade,
    idx        integer not null,
    name       text,
    status     text not null default 'pending',    -- pending|running|done
    output_ref text,
    primary key (project_id, idx)
);

-- Saved output after each phase so work is never lost / restarted.
create table if not exists public.checkpoints (
    project_id uuid not null references public.projects (id) on delete cascade,
    phase_idx  integer not null,
    output     jsonb,
    saved_at   timestamptz not null default now(),
    primary key (project_id, phase_idx)
);

-- Execution ledger: powers provider spend, avg success, dashboard.
create table if not exists public.execution_logs (
    id         uuid primary key default gen_random_uuid(),
    user_id    uuid,
    project_id uuid,
    provider   text,
    phase_idx  integer,
    tokens     integer,
    cost_usd   numeric,
    success    boolean,
    month      text,                                -- 'YYYY-MM' (UTC)
    created_at timestamptz not null default now()
);
create index if not exists execution_logs_month_idx on public.execution_logs (month);
