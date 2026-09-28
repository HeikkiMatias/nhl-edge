-- Final regular-season NHL games, one row per game (issue #4). Mirrors the lake's games table
-- (src/nhl_edge/lake/schemas.py Games); the nightly ingest upserts yesterday's games.
-- Scores are full-game: a shootout adds one goal for its winner, so home_score > away_score
-- settles the moneyline, OT and shootout included. observed_utc is when the result counts as
-- public: start_utc plus six hours, since the NHL API has no end time (ADR 0003).

create table public.games (
  game_id bigint primary key,
  season integer not null check (season % 10000 = season / 10000 + 1),
  game_date date not null,
  start_utc timestamptz not null,
  home text not null check (home ~ '^[A-Z]{3}$'),
  away text not null check (away ~ '^[A-Z]{3}$'),
  venue text not null,
  home_score smallint not null check (home_score >= 0),
  away_score smallint not null check (away_score >= 0),
  decided_in text not null check (decided_in in ('REG', 'OT', 'SO')),
  neutral_site boolean not null,
  limited_attendance boolean not null,
  observed_utc timestamptz not null,
  raw_key text not null,
  constraint games_home_is_not_away check (home <> away),
  constraint games_no_ties check (home_score <> away_score),
  constraint games_extra_time_wins_by_one
    check (decided_in = 'REG' or abs(home_score - away_score) = 1),
  constraint games_regular_season_id_of_its_season
    check (game_id / 1000000 = season / 10000 and (game_id / 10000) % 100 = 2),
  constraint games_observed_after_start check (observed_utc > start_utc)
);

comment on table public.games is
  'Final regular-season NHL games. Full-game scores (shootout winner +1); observed_utc is when '
  'the result counts as public (start + 6h, ADR 0003).';

-- Games by season and date: the dashboard's schedule view and date-range reads.
create index games_season_game_date_idx on public.games (season, game_date);

-- Only the pipeline's service role reads and writes. RLS stays on with no policies, so the
-- anon and authenticated roles see nothing even if a grant is added by mistake.
alter table public.games enable row level security;
revoke all on table public.games from anon, authenticated;
grant select, insert, update, delete on table public.games to service_role;
