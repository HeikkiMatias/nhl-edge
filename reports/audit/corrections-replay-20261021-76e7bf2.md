# Corrections replay (#173)

`corrections-replay-20261021-76e7bf2`. A retrospective sensitivity check of ADR 0004: it never enters the ledger, the live counts or any fit, and no result in it is read to prefer either version.

## The corrected version

- **Games:** 105 games played from 2026-09-29 to 2026-10-13 have both copies of their feeds. Their rows in the seven feed tables are replaced by rows parsed from the week-later copies, through the ingest's own parser.
- **Shift charts:** 64 of them keep their first chart, because the rechecked chart came back empty (#237).

| Feed table | Base rows | Corrected rows |
| --- | ---: | ---: |
| shots | 8,653 | 8,650 |
| shifts | 79,006 | 79,000 |
| actual_lineups | 4,200 | 4,200 |
| shift_coverage | 105 | 105 |
| strength_time | 1,088 | 1,090 |
| penalties | 879 | 878 |
| faceoffs | 6,091 | 6,091 |

## Inputs

Both versions ran the live build's ten steps over the season. Every feature table's rows from 2026-09-29 on are compared, row by row. shot_xg and stints are the games' own scored shots and stints. The other tables are each game's pre-game rows, which a correction to an earlier game can reach.

| Table | Rows | Games | Rows changed | Games changed | Rows added | Rows removed |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| shot_xg | 12,491 | 151 | 10 | 9 | 12 | 15 |
| stints | 56,112 | 143 | 1,429 | 28 | 1 | 6 |
| team_strength | 153 | 153 | 143 | 143 | 0 | 0 |
| goalie_starts | 948 | 153 | 0 | 0 | 0 | 0 |
| lineups | 8,763 | 153 | 794 | 33 | 0 | 0 |
| lineup_replacements | 612 | 153 | 0 | 0 | 0 | 0 |
| goalie_effects | 948 | 153 | 909 | 146 | 0 | 0 |
| schedule_terms | 153 | 153 | 0 | 0 | 0 | 0 |
| player_ratings | 31,260 | 153 | 28,816 | 146 | 0 | 0 |
| rapm_terms | 3,563 | — | 2,623 | — | 0 | 0 |
| penalty_rates | 15,630 | 153 | 8,750 | 86 | 0 | 0 |
| expected_power_plays | 306 | 153 | 216 | 108 | 0 | 0 |
| finishing | 7,815 | 153 | 7,474 | 146 | 0 | 0 |
| goal_multipliers | 948 | 153 | 909 | 146 | 0 | 0 |

| Table | Column | Rows changed | Median absolute change | Largest |
| --- | --- | ---: | ---: | ---: |
| shot_xg | xg | 10 | 0.08405 | 0.1905 |
| stints | period | 7 | 1 | 1 |
| stints | start_s | 1,388 | 4 | 105 |
| stints | end_s | 1,395 | 4 | 105 |
| stints | seconds | 1,202 | 7 | 95 |
| stints | home_skaters | 780 | — | — |
| stints | away_skaters | 849 | — | — |
| stints | home_goalie | 11 | — | — |
| stints | away_goalie | 3 | — | — |
| stints | strength | 52 | — | — |
| stints | score_state | 21 | 1 | 1 |
| stints | zone_start | 276 | — | — |
| stints | home_xg | 238 | 0.05865 | 0.5002 |
| stints | away_xg | 203 | 0.06683 | 0.5955 |
| stints | home_goals | 16 | 1 | 1 |
| stints | away_goals | 26 | 1 | 1 |
| stints | drop_reason | 3 | — | — |
| team_strength | delta_s | 142 | 0.0002513 | 0.002623 |
| team_strength | delta_5v5 | 98 | 0.0002441 | 0.0008348 |
| team_strength | delta_special_teams | 141 | 7.214e-08 | 0.001807 |
| lineups | exp_5v5 | 794 | 0.0001665 | 0.05761 |
| goalie_effects | effect | 147 | 8.688e-06 | 4.52e-05 |
| goalie_effects | expected_shots | 909 | 0.0002824 | 0.01288 |
| goalie_effects | goals_saved | 883 | 8.063e-07 | 0.001955 |
| player_ratings | mean | 28,320 | 1.152e-05 | 0.004492 |
| player_ratings | prior | 2,600 | 0.0001551 | 0.002409 |
| player_ratings | sd | 28,816 | 1.469e-06 | 6.538e-06 |
| player_ratings | hours | 244 | 0.001094 | 0.008578 |
| rapm_terms | value | 2,623 | 4.824e-05 | 0.0956 |
| rapm_terms | hours | 60 | 0.01277 | 0.01722 |
| penalty_rates | mean | 8,750 | 1.034e-05 | 0.02482 |
| penalty_rates | prior | 8,750 | 4.438e-05 | 4.658e-05 |
| penalty_rates | sd | 8,318 | 8.986e-07 | 0.002681 |
| penalty_rates | hours | 244 | 0.001094 | 0.008578 |
| expected_power_plays | taken_index | 172 | 2.967e-05 | 0.002386 |
| expected_power_plays | drawn_index | 172 | 3.252e-05 | 0.001378 |
| expected_power_plays | opportunities | 172 | 0.0002647 | 0.00376 |
| expected_power_plays | pp_minutes | 172 | 0.0001636 | 0.006006 |
| expected_power_plays | pk_minutes | 172 | 0.0001636 | 0.006006 |
| expected_power_plays | sh_xg | 216 | 6.445e-05 | 7.48e-05 |
| expected_power_plays | league_opportunities | 172 | 0.0003524 | 0.0003631 |
| expected_power_plays | pp_length | 172 | 0.0002161 | 0.0002236 |
| expected_power_plays | sh_xg_per_pk_minute | 216 | 1.231e-05 | 1.28e-05 |
| finishing | phi | 7,403 | 4.333e-06 | 0.00446 |
| finishing | phi_sd | 7,386 | 5.528e-07 | 0.0007589 |
| finishing | expected_goals | 7,403 | 0.0002646 | 0.2362 |
| finishing | xg_rate | 6,985 | 1.392e-06 | 0.04183 |
| finishing | xg_rate_prior | 6,985 | 1.511e-05 | 3.527e-05 |
| finishing | hours | 122 | 0.001094 | 0.008578 |
| finishing | share | 7,402 | 1.185e-07 | 0.001949 |
| finishing | league_finishing | 7,474 | 1.571e-05 | 2.901e-05 |
| goal_multipliers | phi | 909 | 1.099e-05 | 0.0004075 |
| goal_multipliers | gamma | 883 | 6.015e-07 | 0.0006303 |
| goal_multipliers | multiplier | 909 | 1.513e-05 | 0.001007 |
| goal_multipliers | xg_per_shot | 909 | 1.823e-06 | 3.474e-06 |
| goal_multipliers | league_finishing | 909 | 1.57e-05 | 2.901e-05 |

## Predictions on the decision days

Each complete run bundle's day, 13 days from 2026-10-08 to 2026-10-20, decided again with the rows its models read rebuilt from each version. The fits, the live blend, the quotes, the bankroll and a v2 day's starters are the day's own in both.

- **Each day's own bundle:** it still replays the day's ledger, every column, so the official decisions stay reproducible from their own inputs, whatever the lake holds now.
- **Base against the ledger:** the base rows reproduce every day's ledger, every column, floats within 1e-09.
- **Not compared:** 2 slate games not yet played when the lakes were rebuilt, so their rows are the morning build's in both versions.
- **Games:** 96 compared, 96 predicted in both versions, and 0 whose status differs between them (a prediction in one, none in the other).

| Measure | Games | Changed | Median absolute change | 90th percentile | Largest |
| --- | ---: | ---: | ---: | ---: | ---: |
| p_b2 (points) | 96 | 96 | 0.014 | 0.060 | 0.132 |
| p_b3 (points) | 96 | 96 | 0.011 | 0.031 | 0.063 |
| u | 96 | 32 | 1.388e-16 | 9.089e-06 | 0.0002962 |
| p_blend (points) | 96 | 96 | 0.004 | 0.016 | 0.027 |

**Bets, a diagnostic only:** at the day's own prices, 31 bets in the base and 31 in the corrected version. 0 games are bet in one version only, 0 bet in both change side, and 26 stakes change, the largest by 0.005 units of the 100-unit bankroll.

## Limits

- **Size:** 105 corrected games, and 96 predictions on 13 decision days in 3 calendar weeks: too few weekly blocks for a block bootstrap interval (hard rule 7). So the figures are counts and ranges, no statistical verdict is made, and no pass or fail is read from them.
- **History:** the rechecks are prospective, so this can't show how the 2010-26 backfill's late copies differed from first ones.
