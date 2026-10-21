# 0004. Per-game feeds count as public the morning after, corrections included

- Status: Accepted
- Date: 2026-09-29

## Context

Issue #5 parses each game's play-by-play, boxscore and shift chart into `shots`, `shifts`, `actual_lineups` and `shift_coverage`. Hard rule 1 needs an `observed_utc` for every row. Two facts complicate it:

- **No publish time.** The feeds carry only the scheduled start. The live pipeline fetches them with the results in the 09:00 UTC nightly run.
- **Late copies.** The 2010-26 backfill fetched every feed in September 2026, years after the games. Those copies include post-game corrections: a goal credited to another player, and now and then a fixed shot or time-on-ice record. The live pipeline caches its first copy and never refetches it (the `always` reuse rule).

So the backtest reads corrected feeds, and live reads first versions. The leakage audit of #23 asked the parsers to read only fields that corrections leave alone, or to record the choice here.

## Options

1. **Fetch time as `observed_utc`.** It is right for live, but every backfilled row would be observed in 2026, which makes 2010-26 useless for a point-in-time backtest.
2. **10:00 UTC the morning after the game date, as for results (ADR 0003), with the backfilled copies used as fetched.** The tables keep the facts the models need and leave out the scoring credits they do not. The cost is that a backtest can see a corrected value (a goal's scorer, a shot record, a time on ice) where live would still see the first one.
3. **As 2, without the goal scorer.** One correction-prone column fewer. But per-shooter finishing (φ in B3) needs to know who scored, and shot records and time on ice would still carry corrections.
4. **Refetch live feeds N days later, and date correction-prone fields at game date + N.** Backtest and live would match. But N is unknown (corrections can come weeks later), and it adds requests and a second timestamp per table.

## Decision

Option 2. Under hard rule 1 a correction published after the morning-after timestamp is look-ahead: the backtest sees a value live did not have at that time. It is accepted here as small, because a correction records the same past event more accurately and carries no news about any later game. It still favours the backtest, and it is unmeasured (#30). The tables keep:

- **`shots`:** where, when and at what strength (coordinates, shot type, `situationCode`), plus `shooter_id`.
- **`shifts`:** who was on the ice when.
- **`actual_lineups`:** who dressed, in which role, the starting goalie, and time on ice.

Assists, points, plus-minus, penalty minutes, shot totals, the goalie decision and the position code stay out. A leakage test locks each column set, but no test can catch a corrected value in a kept column. Kept columns corrections can change:
- **Goal scorer.** Scoring changes move it most often. It feeds only per-shooter finishing, which is shrunk toward 1 over hundreds of shots, so a moved credit barely changes it.
- **Shot records.** A shot can be added, removed or reclassified (on goal or missed), and its coordinates or type fixed.
- **Time on ice.** It can be fixed in the boxscore and the shift chart.

## Backtest evidence

None yet. The correction rate itself is unmeasured. #30 measures it by refetching two weeks of 2026-27 games 7 days after the nightly copy and diffing the parsed rows, before phase 2's first backtest with shots-based team strength (B2 against B1) relies on this decision.

## Measured (#30, 2026-10-21)

Each of the 89 games due by 2026-10-20 (2026020001 to 2026020089) was fetched again a week after its first copy. Both copies were parsed and compared row by row, in the audit report's corrections section.

| What changed | Games | Rows |
| --- | ---: | ---: |
| Shots added or removed | 14 | 19 |
| A shot's event type or shot type | 13 | 17 |
| A shot's place or time | 4 | 10 |
| A shot's previous-play fields | 15 | 26 |
| A goal's scorer | 0 | 0 |
| Time on ice | 13 | 59 |
| Shifts, in 41 compared charts | 8 | 465 |
| Event order only | 20 | 1,494 |

- **Beyond order:** 39 of the 89 games changed. That is about 1% of 7,349 shot rows and 59 of 3,560 lineup rows.
- **The charts:** 48 rechecked shift charts came back empty (#237), so shifts were compared in only 41 games.
- **The rule below is met:** corrections changed more than scorer credit in more than a few games.
- **The owner's decision (2026-10-21):** this ADR stands. The corrections reach many games but few rows, and are accepted as small. #173 measures their effect on inputs and predictions later. Gate 1 went ahead (ADR 0014).

## Measured at the level of predictions (#173, 2026-10-21)

`nhl audit corrections-replay` rebuilt the live season twice in scratch copies of the lake: as it is, and with the 105 rechecked games' feed rows (2026-09-29 to 10-13) parsed from their week-later copies by the ingest's own parser. Both ran the live build's ten steps. Then each decision day from 2026-10-08 to 10-20 was decided again with each version's rows, under the day's own fits, prices, bankroll and starters (`reports/audit/corrections-replay-20261021-0372cd5.md`).

- **Inputs:** the corrections reach most later games' pre-game rows, but by little.
  - ΔS changes by at most 0.0026, a goalie's effect by at most 0.00005, and a player's rating by at most 0.0045.
  - Goalie starts, lineup replacements and schedule terms don't change.
- **Predictions:** over 96 predictions, the blend's probability moves by a median of 0.004 points, and by at most 0.027. B2 moves by at most 0.13 points, and B3 by at most 0.06.
- **Bets, a diagnostic only:** both versions make the same 31 bets on the same sides. 26 stakes move, by at most 0.005 units of the 100-unit bankroll.
- **Reproducibility:**
  - each day's own bundle still replays its ledger (#171);
  - the base rebuild reproduces production's feature tables row for row, and every ledger.
- **Limits:**
  - three calendar weeks give too few blocks for an interval (hard rule 7), so no statistical verdict is made;
  - 64 of the 105 rechecked shift charts came back empty (#237), so shift corrections are seen in 41 games only;
  - the sample is prospective, so it can't show how the 2010-26 backfill's late copies differed from first ones.
- **Recommendation:** this ADR stands, with this measured limitation. At this size, corrections move predictions by hundredths of a point and change no bet, so nothing here calls for a revision. The replay can be run again as rechecks accumulate.

## Consequences

- Every row of the four tables has `observed_utc = result_public_utc(game_date)`. So does `strength_time` (#72, added later), which `tests/leakage/test_strength_time.py` checks. A game's own shots, shifts and lineup never feed a prediction for it (hard rules 1 and 9). `tests/leakage/test_shots.py`, `test_shifts.py`, `test_actual_lineups.py` and `test_shift_coverage.py` check this.
- A feature that wants assists, points or other scoring credits needs a new decision first. Adding the column also breaks a locked leakage test.
- Live ratings use first versions for the latest games, and the backtest uses corrected ones. This makes the backtest slightly optimistic: #30 counted the corrections, and #173 measured their effect on predictions (above).

## Revisit when

A diff of first and later copies shows corrections changing more than scorer credit: coordinates, strength, on-ice players or time on ice in more than a few games a season. Also revisit if a model component starts using scoring credits.
