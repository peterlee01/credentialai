-- Credentialai physician accounts (Phase 1).
-- Run once in the Supabase dashboard: SQL Editor -> New query -> paste -> Run.

create table if not exists public.profiles (
  id            uuid primary key references auth.users (id) on delete cascade,
  email         text,
  first_name    text not null,
  last_name     text not null,
  state         text not null,
  profession    text not null,
  npi           text not null,
  specialty     text,
  practice_city text,
  -- The physician confirmed this NPI is theirs. Self-attested in Phase 1: no
  -- profile is shown to employers until identity verification exists.
  attested_at   timestamptz not null default now(),
  prefs         jsonb not null default '{}'::jsonb,
  created_at    timestamptz not null default now(),
  updated_at    timestamptz not null default now()
);

-- One NPI can only be claimed by one account.
create unique index if not exists profiles_npi_key on public.profiles (npi);

create table if not exists public.verifications (
  id          uuid primary key default gen_random_uuid(),
  user_id     uuid not null references auth.users (id) on delete cascade,
  created_at  timestamptz not null default now(),
  verdict     text,
  verdict_text text,
  summary     jsonb,
  matches     jsonb,
  report_id   text
);
create index if not exists verifications_user_created on public.verifications (user_id, created_at desc);

-- Row-level security: a signed-in physician can read only their own rows.
-- All writes go through the server (service role), which scopes them to the
-- signed-in user, so no insert/update/delete policies are granted to clients.
alter table public.profiles enable row level security;
alter table public.verifications enable row level security;

drop policy if exists "read own profile" on public.profiles;
create policy "read own profile" on public.profiles for select using (auth.uid() = id);

drop policy if exists "read own verifications" on public.verifications;
create policy "read own verifications" on public.verifications for select using (auth.uid() = user_id);
