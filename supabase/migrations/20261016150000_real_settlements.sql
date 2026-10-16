-- The real bets' settlements for the dashboard's real record (#270, ADR 0039). R2 stays the record
-- (real/live/settlements/, src/nhl_edge/lake/schemas.py RealSettlements): `nhl real settle --r2
-- --supabase` inserts each revision here once, so a rerun sends nothing twice and the first run
-- backfills. A settlement is never rewritten: a correction appends the bet's next revision, and
-- the latest revision of each bet counts. Insert-only, by the service role; read by the
-- dashboard's owners only (public.is_dashboard_owner(), from the paper ledger migration). anon
-- sees nothing.

create table public.real_settlements (
  game_date date not null,
  game_id bigint not null,
  revision integer not null check (revision >= 0),
  season integer not null check (season % 10000 = season / 10000 + 1),
  mode text not null check (mode in ('practice', 'real')),
  venue text not null,
  side text not null check (side in ('home', 'away')),
  matched_price numeric(8, 3) not null check (matched_price > 1),
  stake numeric(10, 2) not null check (stake > 0),
  bet_id text not null,
  status text not null check (status in ('settled', 'void', 'cancelled')),
  home_win smallint check (home_win in (0, 1)),
  won boolean,
  profit double precision not null,
  result_utc timestamptz not null,
  settled_utc timestamptz not null,
  bankroll_after double precision not null,
  code_version text not null,
  created_at timestamptz not null default now(),
  primary key (game_date, game_id, revision),
  constraint real_settlements_of_an_alert foreign key (game_date, game_id)
    references public.real_alerts (game_date, game_id),
  constraint real_settlements_after_the_result check (settled_utc >= result_utc),
  -- On the full game, OT and shootout included, at the matched price and the stake placed.
  constraint real_settlements_profit_follows_the_result check (
    (status in ('void', 'cancelled') and profit = 0)
    or (status = 'settled' and won is not null and abs(profit - case when won
      then stake * (matched_price - 1) else -stake end) < 1e-6)
  )
);

comment on table public.real_settlements is
  'Each settlement revision of a placed real (or practice) bet, mirrored from R2 '
  '(real/live/settlements/). The latest revision of each bet counts.';

alter table public.real_settlements enable row level security;
revoke all on table public.real_settlements from anon, authenticated, service_role;
grant select, insert on table public.real_settlements to service_role;
grant select on table public.real_settlements to authenticated;
create policy real_settlements_owner_reads on public.real_settlements
  for select to authenticated using (public.is_dashboard_owner());
