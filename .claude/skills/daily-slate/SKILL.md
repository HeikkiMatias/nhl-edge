---
name: daily-slate
description: Run today's predictions and list edges, flags and lineup gaps for manual review, with each
  edge attributed to goalie, players, rest or team residual. Skeleton until phase 5.
disable-model-invocation: true
argument-hint: "[date, default today]"
---
Skeleton: `nhl predict` is a stub until phase 5. If it exits with "not implemented yet", say so and stop.

1. Run `uv run nhl status --brief` and confirm today's odds snapshots and lineups have landed.
2. Run `uv run nhl predict` for $ARGUMENTS (default: today, US Eastern game date).
3. List candidate bets that pass the frozen policy: expected return at the executable price of at least
   2.5%, higher when the uncertainty score is high; quarter Kelly with a 1.5% cap per bet and 5% per day;
   at most one bet per game. Show the Pinnacle price, the best EU price and its book.
4. Flag for manual review: probability gaps above 8 points, unconfirmed goalies, availability doubts,
   and games where the model sides against a sharp market move since the morning snapshot (skip those).
5. For every flagged bet, attribute the gap to its inputs: starting goalie, player ratings, rest and
   travel, team residual. An edge explained by one odd input is a suspected bug.
6. List lineup gaps: missing projected players, call-ups, goalies without a start probability.
