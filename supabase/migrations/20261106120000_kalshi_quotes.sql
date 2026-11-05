-- The Kalshi quoting simulator on the dashboard (#398): `nhl real quotes --r2 --supabase` upserts
-- each kept game date's figures per variant here (src/nhl_edge/live/kalshi_quotes.py,
-- day_figures), and the figures over every kept day, with their weekly block bootstrap
-- intervals, as each night's report (summary). Both records are written: the simulator's
-- (kalshi_quotes/1) and the hold rules' (kalshi_quotes/holds/1, #385). The private record in R2
-- stays the record; these are copies, recomputed each night as the games' results come in.
-- Sums only, per game date and variant: no price, ticker, order book, trade or fill time reaches
-- these tables (#280). The owner chose owner-only tables for them on 2026-11-05.
-- Reads are for the dashboard's owners only (public.is_dashboard_owner, the paper ledger
-- migration); the service role writes. Apply it, then set the repository variable
-- SUPABASE_QUOTES to true: the next nightly run copies every day kept since 2026-10-31.

create table public.quote_days (
  game_date date not null,
  record text not null check (record in ('kalshi_quotes/1', 'kalshi_quotes/holds/1')),
  variant text not null,
  fair text not null,
  half_spread_cents smallint not null check (half_spread_cents >= 0),
  hold text,
  games integer not null check (games >= 0),
  watch_logs boolean not null,
  posted integer not null check (posted >= 0),
  pulled_move integer not null check (pulled_move >= 0),
  pulled_starter integer not null check (pulled_starter >= 0),
  fills integer not null check (fills >= 0),
  contracts double precision not null check (contracts >= 0),
  matched_contracts double precision not null check (matched_contracts >= 0),
  largest_inventory double precision not null,
  cost_usd double precision not null,
  fees_usd double precision not null,
  edge_fair_usd double precision not null,
  last_contracts double precision not null,
  edge_last_usd double precision not null,
  close_contracts double precision not null,
  edge_close_usd double precision not null,
  settled_contracts double precision not null,
  payout_usd double precision not null,
  profit_usd double precision,
  quoted_minutes double precision,
  late_minutes double precision,
  kept_utc timestamptz not null,
  code_version text not null,
  written_utc timestamptz not null default now(),
  primary key (game_date, record, variant)
);

comment on table public.quote_days is
  'The Kalshi quoting simulator per game date and variant (#358, #398): its counts, contracts, '
  'cost with the maker fee, the profit expected at posting by the fair value, by Kalshi''s last '
  'mid and by Pinnacle''s close (each net of the fee, on the contracts it is known for), and the '
  'profit at settlement, null until every fill''s game has its result. Paper only: fills are an '
  'upper bound.';
comment on column public.quote_days.edge_fair_usd is
  'The sum over fills of (the fair value at posting less the price) times the contracts, less '
  'the maker fee: the profit the variant''s own fair value expected.';
comment on column public.quote_days.edge_last_usd is
  'The same with Kalshi''s last mid before the start, on last_contracts contracts.';
comment on column public.quote_days.edge_close_usd is
  'The same with Pinnacle''s de-vigged closing proxy observed after the fill, on '
  'close_contracts contracts.';
comment on column public.quote_days.profit_usd is
  'The payout on the full game less the cost with the fee; null until every fill''s game is '
  'settled.';

create table public.quote_reports (
  as_of date not null,
  record text not null check (record in ('kalshi_quotes/1', 'kalshi_quotes/holds/1')),
  figures jsonb not null,
  code_version text not null,
  written_utc timestamptz not null default now(),
  primary key (as_of, record)
);

comment on table public.quote_reports is
  'The simulator''s figures over every kept day as of each night (kalshi_quotes.summary): per '
  'variant its counts and each measure per contract, with its weekly block bootstrap interval '
  'from 4 weeks, counts only before; the hold rules'' paired measure (#385).';

alter table public.quote_days enable row level security;
alter table public.quote_reports enable row level security;
revoke all on table public.quote_days from anon, authenticated, service_role;
revoke all on table public.quote_reports from anon, authenticated, service_role;
-- A day's figures are recomputed each night as its games settle, so the service role may
-- replace them.
grant select, insert, update on table public.quote_days to service_role;
grant select, insert, update on table public.quote_reports to service_role;
grant select on table public.quote_days to authenticated;
grant select on table public.quote_reports to authenticated;
create policy quote_days_owner_reads on public.quote_days
  for select to authenticated using (public.is_dashboard_owner());
create policy quote_reports_owner_reads on public.quote_reports
  for select to authenticated using (public.is_dashboard_owner());
