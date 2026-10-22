-- Credentialai facility accounts (Phase 1: early access).
-- Run once in the Supabase dashboard: SQL Editor -> New query -> paste -> Run.
-- Safe to run again.

-- One row per facility account. A facility signs up itself (approved = false,
-- a few free verifications); you approve it by setting approved = true in
-- Table Editor -> facilities, and can change its monthly_limit there too.
create table if not exists public.facilities (
  id            uuid primary key references auth.users (id) on delete cascade,
  email         text,
  name          text not null,
  facility_type text,
  contact_name  text,
  city          text,
  state         text,
  approved      boolean not null default false,
  monthly_limit integer not null default 25,
  created_at    timestamptz not null default now(),
  updated_at    timestamptz not null default now()
);

-- Every verification a facility runs, so it can reopen past results.
create table if not exists public.facility_runs (
  id                uuid primary key default gen_random_uuid(),
  facility_id       uuid not null references public.facilities (id) on delete cascade,
  created_at        timestamptz not null default now(),
  first_name        text,
  last_name         text,
  state             text,
  profession        text,
  verdict           text,
  verdict_text      text,
  summary           jsonb,
  physician_matches jsonb,
  report_id         text
);
create index if not exists facility_runs_facility_created on public.facility_runs (facility_id, created_at desc);

-- Intro requests a facility sends for a matched physician. You arrange the intro.
create table if not exists public.intro_requests (
  id             uuid primary key default gen_random_uuid(),
  facility_id    uuid not null references public.facilities (id) on delete cascade,
  created_at     timestamptz not null default now(),
  run_id         uuid references public.facility_runs (id) on delete set null,
  physician_npi  text,
  physician_name text,
  note           text,
  status         text not null default 'new'
);

-- Row-level security on; only the server (service role, api/facility.py) reads and writes.
alter table public.facilities enable row level security;
alter table public.facility_runs enable row level security;
alter table public.intro_requests enable row level security;

drop policy if exists "read own facility" on public.facilities;
create policy "read own facility" on public.facilities for select using (auth.uid() = id);
drop policy if exists "read own facility runs" on public.facility_runs;
create policy "read own facility runs" on public.facility_runs for select using (auth.uid() = facility_id);
drop policy if exists "read own intro requests" on public.intro_requests;
create policy "read own intro requests" on public.intro_requests for select using (auth.uid() = facility_id);

revoke all on public.facilities, public.facility_runs, public.intro_requests from anon, authenticated;
grant select, insert, update, delete on public.facilities, public.facility_runs, public.intro_requests to service_role;
