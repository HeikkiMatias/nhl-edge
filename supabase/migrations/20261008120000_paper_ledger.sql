-- The paper ledger for the dashboard (#167, docs/plans/phase-5.md task 8). It mirrors the R2
-- ledgers (ledger/live/<date>.parquet, src/nhl_edge/lake/schemas.py PaperLedger), which stay the
-- record: `nhl live supabase --r2` inserts their rows here and the nightly settlement's columns.
-- - predictions: one row per slate game's decision, its prediction or why it has none.
-- - paper_bets: one row per paper bet, with its settlement filled in once the game is final.
-- Both are insert-only: a decision is never rewritten. Only paper_bets' settlement columns can be
-- updated, and only by the service role, which the pipeline alone holds.
-- Reads are for the dashboard's owner only: the signed-in users listed in dashboard_owners. The
-- owner adds their own auth user id once, after applying this migration:
--   insert into public.dashboard_owners (user_id) values ('<auth.users id>');
-- anon sees nothing.

create table public.dashboard_owners (
  user_id uuid primary key references auth.users (id) on delete cascade,
  added_utc timestamptz not null default now()
);

comment on table public.dashboard_owners is
  'The signed-in users who may read the paper ledger. Added by hand in the SQL editor.';

alter table public.dashboard_owners enable row level security;
revoke all on table public.dashboard_owners from anon, authenticated;

-- Whether the caller is a dashboard owner. Security definer, so the policies can ask without
-- granting anyone a read of dashboard_owners itself.
create function public.is_dashboard_owner() returns boolean
  language sql
  stable
  security definer
  set search_path = ''
as $$
  select exists (select 1 from public.dashboard_owners where user_id = auth.uid());
$$;

revoke all on function public.is_dashboard_owner() from public, anon;
grant execute on function public.is_dashboard_owner() to authenticated;

create table public.predictions (
  id bigint generated always as identity primary key,
  game_date date not null,
  season integer not null check (season % 10000 = season / 10000 + 1),
  game_id bigint not null,
  event_id text,
  start_utc timestamptz not null,
  home text not null check (home ~ '^[A-Z]{3}$'),
  away text not null check (away ~ '^[A-Z]{3}$'),
  prediction_utc timestamptz not null,
  published_utc timestamptz not null,
  status text not null,
  decision_snapshot_utc timestamptz,
  home_price numeric(8, 3) check (home_price > 1),
  away_price numeric(8, 3) check (away_price > 1),
  last_update_utc timestamptz,
  best_home_price numeric(8, 3) check (best_home_price > 1),
  best_home_book text,
  best_away_price numeric(8, 3) check (best_away_price > 1),
  best_away_book text,
  p_b0 double precision check (p_b0 > 0 and p_b0 < 1),
  p_b1 double precision check (p_b1 > 0 and p_b1 < 1),
  p_b2 double precision check (p_b2 > 0 and p_b2 < 1),
  p_b3 double precision check (p_b3 > 0 and p_b3 < 1),
  u double precision,
  u_sd double precision,
  p_blend double precision check (p_blend > 0 and p_blend < 1),
  p_blend_b2 double precision check (p_blend_b2 > 0 and p_blend_b2 < 1),
  p_blend_market double precision check (p_blend_market > 0 and p_blend_market < 1),
  bet boolean not null default false,
  policy_version text not null,
  blend_version text not null,
  feature_build text,
  code_version text not null,
  created_at timestamptz not null default now(),
  -- A game postponed after its decision is decided again on its new date.
  constraint predictions_decision_key unique (game_date, game_id),
  constraint predictions_home_is_not_away check (home <> away),
  -- Every prediction is decided and published before its game starts (#164, #170).
  constraint predictions_before_the_start check (
    status <> 'predicted' or (prediction_utc < start_utc and published_utc < start_utc)
  ),
  constraint predictions_published_after_the_decision check (published_utc >= prediction_utc)
);

comment on table public.predictions is
  'Each slate game''s paper decision at 12:45 ET (nhl predict), mirrored from the R2 ledger. '
  'prediction_utc is the decision instant; every model input was known before it.';

create table public.paper_bets (
  id bigint generated always as identity primary key,
  game_date date not null,
  game_id bigint not null,
  start_utc timestamptz not null,
  home text not null check (home ~ '^[A-Z]{3}$'),
  away text not null check (away ~ '^[A-Z]{3}$'),
  prediction_utc timestamptz not null,
  decision_snapshot_utc timestamptz not null,
  side text not null check (side in ('home', 'away')),
  price numeric(8, 3) not null check (price > 1),
  p_side double precision not null check (p_side > 0 and p_side < 1),
  ev double precision not null,
  hurdle double precision not null,
  fraction double precision not null check (fraction > 0 and fraction <= 0.015),
  bankroll double precision not null check (bankroll > 0),
  stake double precision not null check (stake > 0),
  policy_version text not null,
  blend_version text not null,
  created_at timestamptz not null default now(),
  -- The settlement (nhl live settle), on the full game, OT and shootout included.
  settled_utc timestamptz,
  settlement text check (settlement in ('settled', 'void')),
  won boolean,
  profit double precision,
  close_status text check (close_status in ('proxy', 'no pre-game snapshot', 'stale', 'missing')),
  close_snapshot_utc timestamptz,
  p_close double precision check (p_close > 0 and p_close < 1),
  clv double precision,
  fair_move double precision,
  settle_version text,
  constraint paper_bets_decision_key unique (game_date, game_id),
  constraint paper_bets_of_a_prediction foreign key (game_date, game_id)
    references public.predictions (game_date, game_id),
  constraint paper_bets_home_is_not_away check (home <> away),
  constraint paper_bets_before_the_start check (prediction_utc < start_utc),
  -- A CLV only from a closing proxy taken after the bet's own decision snapshot (ADR 0033).
  constraint paper_bets_close_after_the_decision check (
    clv is null or (close_status = 'proxy' and close_snapshot_utc > decision_snapshot_utc)
  )
);

comment on table public.paper_bets is
  'Each paper bet of the frozen policy at Pinnacle''s 12:45 ET price, mirrored from the R2 '
  'ledger, with its settlement and CLV against Pinnacle''s closing proxy once final.';

-- Insert-only for the pipeline. Only the settlement columns may change, and only through the
-- service role.
alter table public.predictions enable row level security;
alter table public.paper_bets enable row level security;
revoke all on table public.predictions, public.paper_bets from anon, authenticated, service_role;
grant select, insert on table public.predictions, public.paper_bets to service_role;
grant update (
  settled_utc, settlement, won, profit, close_status, close_snapshot_utc, p_close, clv,
  fair_move, settle_version
) on table public.paper_bets to service_role;

-- The dashboard's owner reads both; no one else does.
grant select on table public.predictions, public.paper_bets to authenticated;
create policy predictions_owner_reads on public.predictions
  for select to authenticated using (public.is_dashboard_owner());
create policy paper_bets_owner_reads on public.paper_bets
  for select to authenticated using (public.is_dashboard_owner());
