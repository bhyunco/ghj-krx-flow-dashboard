create table if not exists public.profiles (
  id uuid primary key references auth.users(id) on delete cascade,
  email text,
  created_at timestamptz default now()
);

create table if not exists public.query_jobs (
  id uuid primary key default gen_random_uuid(),
  user_id uuid not null references auth.users(id) on delete cascade,
  status text not null default 'pending',
  market text not null,
  as_of date not null,
  trading_dates jsonb default '[]'::jsonb,
  base_rows integer default 0,
  last_rows integer default 0,
  excel_path text,
  error_message text,
  created_at timestamptz default now(),
  started_at timestamptz,
  completed_at timestamptz
);

create table if not exists public.query_rows (
  id bigint generated always as identity primary key,
  job_id uuid not null references public.query_jobs(id) on delete cascade,
  user_id uuid not null references auth.users(id) on delete cascade,
  stock_code text,
  stock_name text,
  buyer text,
  period_label text,
  period_start integer,
  period_end integer,
  sell_volume bigint,
  buy_volume bigint,
  net_buy_volume bigint,
  sell_value bigint,
  buy_value bigint,
  net_buy_value bigint,
  created_at timestamptz default now()
);

create table if not exists public.query_summaries (
  id uuid primary key default gen_random_uuid(),
  job_id uuid not null references public.query_jobs(id) on delete cascade,
  user_id uuid not null references auth.users(id) on delete cascade,
  top_rows jsonb default '[]'::jsonb,
  chart_data jsonb default '[]'::jsonb,
  pivot_columns jsonb default '[]'::jsonb,
  created_at timestamptz default now()
);

alter table public.profiles enable row level security;
alter table public.query_jobs enable row level security;
alter table public.query_rows enable row level security;
alter table public.query_summaries enable row level security;

drop policy if exists "Users can read own profile" on public.profiles;
create policy "Users can read own profile"
on public.profiles for select
using (auth.uid() = id);

drop policy if exists "Users can read own jobs" on public.query_jobs;
create policy "Users can read own jobs"
on public.query_jobs for select
using (auth.uid() = user_id);

drop policy if exists "Users can read own rows" on public.query_rows;
create policy "Users can read own rows"
on public.query_rows for select
using (auth.uid() = user_id);

drop policy if exists "Users can read own summaries" on public.query_summaries;
create policy "Users can read own summaries"
on public.query_summaries for select
using (auth.uid() = user_id);

create or replace function public.handle_new_user()
returns trigger
language plpgsql
security definer
as $$
begin
  insert into public.profiles (id, email)
  values (new.id, new.email)
  on conflict (id) do nothing;

  return new;
end;
$$;

drop trigger if exists on_auth_user_created on auth.users;
create trigger on_auth_user_created
after insert on auth.users
for each row execute function public.handle_new_user();
