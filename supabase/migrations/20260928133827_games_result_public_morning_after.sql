-- ADR 0003 revised (PR #23, Codex P0): a result counts as public at 10:00 UTC the morning after
-- its game date, not six hours after the scheduled start, since a delayed game can finish much
-- later (Lake Tahoe, 2021-02-20, ended about 11 hours after its start). The table is still empty,
-- so the constraint changes in place. observed_utc must stay at least six hours after the start.

alter table public.games drop constraint games_observed_after_start;
alter table public.games add constraint games_observed_six_hours_after_start
  check (observed_utc >= start_utc + interval '6 hours');

comment on table public.games is
  'Final regular-season NHL games. Full-game scores (shootout winner +1); observed_utc is when '
  'the result counts as public: 10:00 UTC the morning after game_date (ADR 0003).';
