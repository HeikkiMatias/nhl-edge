# 0004. Per-game feeds count as public the morning after, corrections included

- Status: Proposed
- Date: 2026-09-29

## Context

Issue #5 parses each game's play-by-play, boxscore and shift chart into `shots`, `shifts`, `actual_lineups` and `shift_coverage`. Hard rule 1 needs an `observed_utc` for every row. Two facts complicate it:

- **No publish time.** The feeds carry only the scheduled start. The live pipeline fetches them with the results in the 09:00 UTC nightly run.
- **Late copies.** The 2010-26 backfill fetched every feed in September 2026, years after the games. Those copies include post-game corrections: a goal credited to another player, and now and then a fixed shot or time-on-ice record. The live pipeline caches its first copy and never refetches it (the `always` reuse rule).

So the backtest reads corrected feeds, and live reads first versions. The leakage audit of #23 asked the parsers to read only fields that corrections leave alone, or to record the choice here.

## Options

1. **Fetch time as `observed_utc`.** It is right for live, but every backfilled row would be observed in 2026, which makes 2010-26 useless for a point-in-time backtest.
2. **10:00 UTC the morning after the game date, as for results (ADR 0003), with the backfilled copies used as fetched.** The tables keep the fields corrections leave alone, plus the goal scorer. The cost is that a backtest credits a few goals to their corrected scorer, where live would still see the first one.
3. **As 2, without the goal scorer.** Every column would be free of corrections. But per-shooter finishing (φ in B3) needs to know who scored.
4. **Refetch live feeds N days later, and date correction-prone fields at game date + N.** Backtest and live would match. But N is unknown (corrections can come weeks later), and it adds requests and a second timestamp per table.

## Decision

Option 2. A correction records a past event more accurately. It carries no news about any later game, so it is not look-ahead. It is only a small gap between backtest and live inputs, in the backtest's favor. The tables keep:

- **`shots`:** where, when and at what strength (coordinates, shot type, `situationCode`), plus `shooter_id`.
- **`shifts`:** who was on the ice when.
- **`actual_lineups`:** who dressed, in which role, the starting goalie, and time on ice.

Assists, points, plus-minus, penalty minutes, shot totals, the goalie decision and the position code stay out. A leakage test locks each column set. The goal scorer is the one kept field that corrections move. It feeds only per-shooter finishing, which is shrunk toward 1 over hundreds of shots, so a moved credit barely changes it.

## Backtest evidence

None yet. The correction rate itself is unmeasured. It can be measured by refetching a sample of recent games a week after the nightly copy and diffing the two. Phase 2's first backtest with shots-based team strength (B2 against B1) is the first run that relies on this decision.

## Consequences

- Every row of the four tables has `observed_utc = result_public_utc(game_date)`. A game's own shots, shifts and lineup never feed a prediction for it (hard rules 1 and 9). `tests/leakage/test_shots.py`, `test_shifts.py`, `test_actual_lineups.py` and `test_shift_coverage.py` check this.
- A feature that wants assists, points or other scoring credits needs a new decision first. Adding the column also breaks a locked leakage test.
- Live finishing ratings use first-version credits for the latest games, and the backtest uses corrected ones. This makes the backtest very slightly optimistic for B3 only.

## Revisit when

A diff of first and later copies shows corrections changing more than scorer credit: coordinates, strength, on-ice players or time on ice in more than a few games a season. Also revisit if a model component starts using scoring credits.
