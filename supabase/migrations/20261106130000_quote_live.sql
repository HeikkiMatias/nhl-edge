-- The live view of market making on the dashboard (#398 step 3): `nhl real quotes-live --r2
-- --supabase`, beside the watch, upserts the quoting simulator's day as it stands every few
-- minutes (src/nhl_edge/live/quotes_live.py): per game, team and variant, our fair value,
-- Kalshi's latest best bid and ask, our resting bid, what we hold and at what cost, and the
-- game's state. Each cycle replaces its rows; the nightly record (quote_days) stays the record.
-- The rows carry Kalshi's prices, so the table is owner-only: the owner chose on 2026-11-05 to see
-- the live view with Kalshi's prices on the dashboard. No ticker, trade or order reaches it (#280).
-- Reads are for the dashboard's owners only (public.is_dashboard_owner); the service role writes.
-- Apply it, then set the repository variable SUPABASE_QUOTES_LIVE to true.

create table public.quote_live (
  game_date date not null,
  variant text not null,
  half_spread_cents smallint not null check (half_spread_cents >= 0),
  game_id bigint not null,
  team text not null,
  home boolean not null,
  opponent text not null,
  start_utc timestamptz not null,
  state text not null check (
    state in (
      'waiting', 'quoting', 'pulled_move', 'pulled_starter', 'not_quoting', 'stopped', 'started'
    )
  ),
  fair double precision check (fair is null or (fair > 0 and fair < 1)),
  yes_bid double precision,
  yes_ask double precision,
  top_utc timestamptz,
  bid_price double precision,
  bid_left double precision,
  contracts double precision not null check (contracts >= 0),
  cost_usd double precision not null,
  fills integer not null check (fills >= 0),
  updated_utc timestamptz not null,
  code_version text not null,
  written_utc timestamptz not null default now(),
  primary key (game_date, variant, game_id, team)
);

comment on table public.quote_live is
  'The quoting simulator''s day as it stands, every few minutes while the watch runs (#398): per '
  'game, team and variant, our fair value, Kalshi''s latest top, our resting bid and what we '
  'hold. Paper only: fills are an upper bound.';
comment on column public.quote_live.state is
  'waiting (no fair value yet), quoting, pulled_move or pulled_starter (held after a pull), '
  'not_quoting (a fair value but no bid: no fresh top or book, or a cap), stopped (from 10 '
  'minutes before the start) or started.';

alter table public.quote_live enable row level security;
revoke all on table public.quote_live from anon, authenticated, service_role;
-- Each cycle replaces its rows, so the service role may update them.
grant select, insert, update on table public.quote_live to service_role;
grant select on table public.quote_live to authenticated;
create policy quote_live_owner_reads on public.quote_live
  for select to authenticated using (public.is_dashboard_owner());
