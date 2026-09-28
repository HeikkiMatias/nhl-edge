-- Live Odds API quotes, one row per snapshot, event, book, market and side (issue #3).
-- Supabase holds the live window only; the raw responses in R2 keep the full history.
-- h2h is the two-way moneyline, settled on the full game including OT and the shootout.
-- h2h_3_way is the regulation line (home, draw, away over 60 minutes) that many EU books quote
-- under the Odds API h2h key. The two must never be compared.

create table public.odds_snapshots (
  id bigint generated always as identity primary key,
  snapshot_utc timestamptz not null,
  last_update_utc timestamptz not null,
  event_id text not null,
  commence_time_utc timestamptz not null,
  home text not null check (home ~ '^[A-Z]{3}$'),
  away text not null check (away ~ '^[A-Z]{3}$'),
  book text not null,
  market text not null check (market in ('h2h', 'h2h_3_way', 'spreads', 'totals')),
  side text not null,
  line numeric(5, 1),
  price_decimal numeric(8, 3) not null check (price_decimal > 1),
  is_closing_proxy boolean not null default false,
  slot text not null,
  raw_key text not null,
  constraint odds_snapshots_quote_key unique (snapshot_utc, event_id, book, market, side),
  constraint odds_snapshots_home_is_not_away check (home <> away),
  constraint odds_snapshots_line_only_for_lines
    check ((market in ('h2h', 'h2h_3_way')) = (line is null)),
  constraint odds_snapshots_side_fits_market check (
    (market in ('h2h', 'spreads') and side in ('home', 'away'))
    or (market = 'h2h_3_way' and side in ('home', 'draw', 'away'))
    or (market = 'totals' and side in ('over', 'under'))
  )
);

comment on table public.odds_snapshots is
  'Live Odds API quotes. snapshot_utc is when the quote was observed (point-in-time filters use '
  'it); last_update_utc is when the book last changed the market.';

-- Price history of one game's market at one book, the path of the closing-proxy lookup.
create index odds_snapshots_event_market_book_idx
  on public.odds_snapshots (event_id, market, book, snapshot_utc);

-- Only the pipeline's service role reads and writes. RLS stays on with no policies, so the
-- anon and authenticated roles see nothing even if a grant is added by mistake.
alter table public.odds_snapshots enable row level security;
revoke all on table public.odds_snapshots from anon, authenticated;
grant select, insert, update, delete on table public.odds_snapshots to service_role;
