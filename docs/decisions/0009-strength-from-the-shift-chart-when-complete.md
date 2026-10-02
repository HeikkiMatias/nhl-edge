# 0009. Skater counts from the shift chart when the chart is complete

- Status: Accepted
- Date: 2026-09-30

## Context

`shots` takes each shot's skater counts, and so `strength`, `skaters_for` and `skaters_against`, from the play-by-play's `situationCode` (ADR 0004). In some games of 2019-20 and 2020-21, `situationCode` loses track of a penalty and stays one skater off for the rest of the game, while the shift chart shows plausible states throughout (#28). The xG model (#73) and team strength (#74) split shots by strength state, and the time-at-strength table (#72) needs the same source. So the source has to be chosen before phase 2 builds on it.

`shift_coverage` compares the two sources at every unblocked shot except penalty shots. In games whose chart is complete (every dressed player's shifts add up to his time on ice), the share of shots where the skater counts differ, over the seasons `nhl audit shifts` reports (2010-11 to 2024-25, 2022-23 included as development evidence under plan §5):

| Seasons | Shots where the chart and `situationCode` differ | Games above 5% |
| --- | ---: | ---: |
| 2010-11 to 2018-19 | 0.09% to 0.22% per season | 5 in all |
| 2019-20 | 3.82% (about 3,550 shots) | 113 |
| 2020-21 | 0.97% (about 690 shots) | 27 |
| 2021-22 to 2024-25 | 0.18% to 0.24% per season | 0 |

Outside the drift, most differences fall at a line change, where either source can be a second off. The goalie digits agree with the chart, so `is_empty_net` is not in question.

A first draft of this table also showed 2025-26, the one-time hockey test season, which design evidence must leave out: a comparison of the two sources, no results, seen on 2026-09-30 while choosing between the options. The figures above leave it out. It changed no number the options rest on, and gate 2 (plan §6) should be judged knowing it was seen.

## Options

1. **Skater counts from the shift chart when the game's chart is complete, `situationCode` otherwise.** This fixes the drift in 140 games. It moves 0.1% to 0.2% of other seasons' shots to the chart's count, where neither source is clearly right. 20 drifted games with an incomplete chart keep the drift.
2. **As 1, and drop those 20 games from strength-dependent fits.** Cleaner in 2019-21, but it adds a drop rule for about 1% of those seasons' games.
3. **`situationCode` everywhere, and drop the games above 5% from strength-dependent fits.** The simplest code, but it throws away about 160 games the chart could fix.

## Decision

Option 1, chosen by the owner on 2026-09-30. A complete chart accounts for every player's time on ice, so its on-ice count is the better record where the two disagree for long stretches. The 20 incomplete drifted games are too few to need a rule of their own.

The rule, per shot:
- **Where the counts come from.** A shot takes the chart's skater counts when:
  - its game's chart is complete;
  - it has a `situationCode` and is not a penalty shot, the shots `shift_coverage` compares;
  - and each team has 3 to 6 skaters on the ice by the chart.

  Otherwise it keeps `situationCode`'s counts.
- **What the chart does not change.** `is_empty_net` stays with `situationCode`'s goalie digits. The raw `situation_code` column is kept as it is.
- **The record.** A new column, `strength_source`, says which source each shot's counts came from.

`shift_coverage` keeps comparing the chart with `situationCode`, so its counts do not change.

Measured on the raw cache with the rule in place, shots whose strength changes from `situationCode`'s:

| Season | Shots | From the chart | Strength changed | Games above 5% changed |
| --- | ---: | ---: | ---: | ---: |
| 2018-19 | 110,231 | 109,247 | 113 (0.10%) | 0 |
| 2019-20 | 93,140 | 90,441 | 3,533 (3.79%) | 113 |
| 2020-21 | 71,311 | 69,162 | 667 (0.94%) | 25 |
| 2021-22 | 113,290 | 110,968 | 214 (0.19%) | 0 |

## Backtest evidence

None yet. xG (#73) and team strength (#74) are the first components to read `strength`. The first B2 backtest (#78) reports B2 with strength from `situationCode` beside it as a sensitivity.

## Consequences

- `ingest/shots.py` and the ingest take the chart's counts after `shift_coverage` rates the chart. `shots` gains `strength_source`, and `nhl ingest --replay` rebuilds every season.
- #72's time-at-strength table uses the same source, the chart for complete games and `situationCode` otherwise.
- RAPM (phase 3) builds its stints from the chart already. It was to drop those that contradict the strength state (plan §9); ADR 0015 drops only those whose counts are impossible, since this ADR trusts the chart over `situationCode`.
- `tests/leakage/test_shots.py` keeps holding every row to the morning after its game. The source is the game's own feeds, which ADR 0004 already dates.
- Through the completeness check, a shot's strength now also depends on the shift chart and the boxscore's time on ice. Both can change after the morning after: the nightly lookback fetches an incomplete chart again for three days, and the backfill read later copies. So a corrected time on ice can move a whole game's shots between the two sources. ADR 0004 accepts such corrections as small, and #30's recheck report counts changed `strength`, `skaters_for` and `skaters_against` values, the fields ADR 0004's revisit trigger names.

## Revisit when

- **A drifted game with an incomplete chart turns up in a season that may inform design:** one `nhl audit shifts` reports by default, not 2025-26 or a live season.
- **Or the chart itself proves wrong over long stretches.** For example, a complete chart whose on-ice counts contradict `situationCode` for a whole period in a season without drift.
