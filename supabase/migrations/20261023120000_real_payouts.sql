-- Kalshi's own payouts in the dashboard's real record (#290, ADR 0040). Apply it before the venue
-- commit makes Kalshi the real bets' venue.
-- - real_settlements.status 'payout': a Kalshi bet held for Kalshi's own resolution, its game
--   played over two weeks from its listed start, or never played and resolved at a fair price,
--   settled at Kalshi's payout once the nightly check has read it. It is no result on the full
--   game, so won and home_win are null, and its profit runs from losing the stake to winning it.
--   The pipeline writes it, as every settlement, from the record in R2.

alter table public.real_settlements
  drop constraint real_settlements_status_check,
  add constraint real_settlements_status_check check (
    status in ('settled', 'void', 'cancelled', 'payout')
  ),
  drop constraint real_settlements_profit_follows_the_result,
  -- On the full game, OT and shootout included, at the matched price and the stake placed; a
  -- payout from nothing to a contract's full dollar.
  add constraint real_settlements_profit_follows_the_result check (
    (status in ('void', 'cancelled') and profit = 0)
    or (status = 'settled' and won is not null and abs(profit - case when won
      then stake * (matched_price - 1) else -stake end) < 1e-6)
    or (status = 'payout' and won is null and home_win is null
      and profit >= -stake - 1e-6 and profit <= stake * (matched_price - 1) + 1e-6)
  );

comment on column public.real_settlements.status is
  '''settled'' on the full game, ''void'' (a sportsbook''s postponed game), ''cancelled'' by a '
  'correction, or ''payout'': a Kalshi bet paid out at Kalshi''s own price (#290).';
