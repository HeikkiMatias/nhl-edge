-- The dashboard's owners read each game's final score (#210, ADR 0034): a settled paper bet shows
-- its score and whether the game ended in regulation, overtime or a shootout. Only the result
-- columns, and only for the users in dashboard_owners (public.is_dashboard_owner(), from the
-- paper ledger migration, which comes first). Writes stay the pipeline's service role alone.

grant select (game_id, game_date, home, away, home_score, away_score, decided_in)
  on table public.games to authenticated;
create policy games_owner_reads on public.games
  for select to authenticated using (public.is_dashboard_owner());
