# Model card

Current production model, how it scored, and its known weaknesses. It is updated only through the `run-backtest` skill, and the accepted metrics live in `reports/backtest/accepted.json`.

## Current model

None. The backtest has the two market baselines, phase 2's model and phase 3's:
- **B0** is the de-vigged SBR moneyline, with nothing fitted, under the default multiplicative method (ADR 0008).
- **B1** is the recalibrated market, a logistic regression on B0's log-odds fitted per test season on the earlier seasons.
- **B2** is the team and goalie model (ADR 0013). It is an L2 logistic regression on team strength, goalie effects, rest and travel and empty seats, with the season home edge as a fixed term. Each fold fits it on earlier games with their actual starters. It predicts by averaging over the goalie-start model's pairs of starters, and reads no price. Gate 1 (#79) judges it against B1.
- **B3** is the player layer (ADR 0023). It is B2's model with one input, Δĝ, in place of team strength and goalie effects. Δĝ is the home team's expected goals less the away team's, built from:
  - the projected lineups and their minutes;
  - RAPM ratings and league rates;
  - expected power plays;
  - finishing and the opposing goalie's conversion.

  It keeps h_s, rest, travel, empty seats, B2's L2 of 100 and B2's mixture over goalie pairs. Each fold trains it on the projected skaters and the starters who played. It reads no price. Gate 2 carried it forward as phase 4's model (ADR 0024), though the one-time 2025-26 test did not confirm its gain over B2.

- **The blend** (phase 4, ADR 0027) is the de-vigged market and B3 combined by a logistic regression whose model weight moves with the uncertainty score u (ADR 0026). It is fitted per whole-season fold on out-of-sample predictions of the folds before it, so on history it scores 2021-22 only. A twin on B2 and a market-only control are fitted beside it. The selection and staking policy (ADR 0028) and the market move guard (ADR 0029) bet with it. Live, the policy bets with the blend fitted on SBR's close (E1), at Pinnacle's 12:45 ET price (ADR 0030).

The run is backtest `backtest-20261004-6f74840` on the development seasons 2018-19 and 2021-22, reproduced with every figure identical by `backtest-20261005-b7639f5`, whose files are committed with the gaps' prediction times. E2 refuses implausible SBR openers (ADR 0007). B0 to B3 are unchanged from `backtest-20261003-b1a7b04`.

## Metrics

Every metric is reported with a 95% weekly block bootstrap interval, pooled over test seasons and per season.

| Metric | B0 | B1 | B2 | B3 | Blend |
| --- | --- | --- | --- | --- | --- |
| Log loss, E1 (close) | 0.6571 [0.6469, 0.6668] | 0.6567 [0.6456, 0.6670] | 0.6679 [0.6607, 0.6749] | 0.6600 [0.6521, 0.6678] | 0.6397 [0.6247, 0.6545], 2021-22 only |
| Log loss, E2 (opener) | 0.6603 [0.6505, 0.6698] | 0.6598 [0.6491, 0.6702] | 0.6677 [0.6605, 0.6748] | 0.6598 [0.6519, 0.6675] | 0.6410 [0.6262, 0.6559], 2021-22 only |
| Paired log-loss difference against B1, E1 | +0.0004 [-0.0005, +0.0013] | reference | +0.0112 [+0.0044, +0.0185] | +0.0033 [-0.0013, +0.0077] | -0.0015 [-0.0043, +0.0018] |
| Paired log-loss difference against B1, E2 | +0.0005 [-0.0006, +0.0015] | reference | +0.0078 [+0.0013, +0.0146] | -0.0000 [-0.0047, +0.0043] | -0.0042 [-0.0078, -0.0007] |
| Paired log-loss difference against B2, E1 | | | reference | -0.0079 [-0.0130, -0.0028] | -0.0018 [-0.0040, +0.0005] against the B2 blend |
| Paired log-loss difference against B2, E2 | | | reference | -0.0078 [-0.0130, -0.0028] | -0.0033 [-0.0062, -0.0002] against the B2 blend |
| Calibration intercept | | | E1 -0.098 [-0.211, +0.015]; E2 -0.099 [-0.209, +0.013] | E1 -0.088 [-0.194, +0.020]; E2 -0.089 [-0.194, +0.018] | E1 +0.021 [-0.138, +0.170]; E2 +0.029 [-0.127, +0.180] |
| Calibration slope | | | E1 1.35 [1.09, 1.62]; E2 1.35 [1.10, 1.62] | E1 1.34 [1.11, 1.58]; E2 1.34 [1.12, 1.58] | E1 1.16 [0.95, 1.39]; E2 1.17 [0.94, 1.41] |
| E3 CLV under the frozen policy | | | | | -1.24% [-2.15%, -0.24%] per bet against SBR's close, not Pinnacle's (465 bets, 2021-22) |

## Artifact versions

| Component | Version | train_cutoff |
| --- | --- | --- |
| Backtest (B0 to B3 and the blend; E2 refuses implausible openers, with every opener as a sensitivity) | backtest-20261004-6f74840 | per fold, below |
| Blend, 2021-22 fold (E1 and E2; ADR 0027), learning from 2018-19 to 2020-21 | backtest-20261004-6f74840 | 2021-05-20 10:00 UTC |
| The frozen policy: u, the blend, selection, staking and the guard (ADR 0026 to 0030) | policy-20261005-8ec5cf3, see "The freeze" below | the blend per fold; θ from 2011-12 to 2017-18 prices |
| B0 | the de-vigged market (multiplicative, ADR 0008) | none: B0 fits nothing |
| B1, 2018-19 fold (E1 and E2) | backtest-20261003-b1a7b04 | 2018-04-09 10:00 UTC |
| B1, 2021-22 fold (E1 and E2) | backtest-20261003-b1a7b04 | 2021-05-20 10:00 UTC |
| B2, 2018-19 fold (E1 and E2), L2 100 tuned by b2-20261001-fe11def | backtest-20261003-b1a7b04 | 2018-04-09 10:00 UTC |
| B2, 2021-22 fold (E1 and E2) | backtest-20261003-b1a7b04 | 2021-05-20 10:00 UTC |
| B3, 2018-19 fold (E1 and E2), B2's L2 100 | backtest-20261003-b1a7b04 | 2018-04-09 10:00 UTC |
| B3, 2021-22 fold (E1 and E2) | backtest-20261003-b1a7b04 | 2021-05-20 10:00 UTC |
| B2 and B3, hockey validation folds 2023-24 and 2024-25 | backtest-hockey-20261003-93d0f92 | 2023-04-15 and 2024-04-19 10:00 UTC |
| B2 and B3, the one-time 2025-26 fold | backtest-hockey-20261003-82fbada | 2025-04-18 10:00 UTC |
| Lineup projection and minutes (ADR 0017, 0018) | lineup-20261002-6297f8c | per season, before its first game |
| RAPM ratings and league rates (ADR 0019, 0020, 0011) | rapm-20261003-86a736f | 2018-04-09 10:00 UTC (tuning) |
| Penalty rates and expected power plays (ADR 0021) | power-plays-20261003-86a736f | 2018-04-09 10:00 UTC (tuning), then per season |
| Finishing and goal multipliers (ADR 0022) | finishing-20261003-f87acde | 2018-04-09 10:00 UTC (tuning), then per season |
| Team strength ΔS (ADR 0011) | team-strength-20261001-1b2a5b8 | 2018-04-09 10:00 UTC (tuning) |
| Goalie-start model (ADR 0012) | goalie-start-20261001-784c4fb | per season, before its first game |
| Goalie effects ΔG (ADR 0011) | goalie-effect-20261001-c05c300 | 2018-04-09 10:00 UTC (tuning) |
| Schedule terms and home edge h_s (ADR 0011) | schedule-terms-20261001-84bc182 | 2018-04-09 10:00 UTC (tuning) |
| xG (ADR 0010) | xg-20261001-de27a2c | per season, before its first game |

## The freeze

The policy was frozen on 2026-10-05 as **`policy-20261005-8ec5cf3`** (#144). It is recorded as `POLICY_VERSION`, `FROZEN_ON` and `LIVE_BLEND_EXPERIMENT` in `betting/selection.py`.
- **When:** after the owner accepted ADRs 0026 to 0030, and after three checks of `backtest-20261004-6f74840` found no bug: the leakage check, the backtest review and the hand review of the blend's gaps.
  - The backtest review found two places where the live policy wasn't fixed. ADR 0030 settles both.
- **What is frozen:** the code on main at 8ec5cf3 in `game/uncertainty.py`, `market/blend.py` and `betting/`, with ADR 0030's live inputs:
  - **u:** its three equally weighted parts, standardized on each fold's training games (ADR 0026). Live goalie doubt reads the goalie-start model, never a confirmation, as on history (ADR 0030).
  - **The blend:** its form and its unpenalized fit on earlier out-of-sample folds. Only its four weights are refit (ADR 0027).
    - Live bets use the E1 fit, learned against SBR's close. It trains on 2018-19 to 2021-22, plus 2022-23 once its one run has scored it, and is fed Pinnacle's 12:45 ET price (ADR 0030).
  - **Selection and staking (ADR 0028):**
    - a hurdle of 2.5% plus 1 point per standard deviation of u above average;
    - a quarter of Kelly, divided by (1 + u⁺);
    - caps of 1.5% per bet and 5% per day;
    - 100 units a season.
  - **Live bets:** placed at the 12:45 ET snapshot at Pinnacle's price, with the best EU book logged beside it (ADR 0028).
  - **The guard:** θ = 0.0535, between Pinnacle's 07:05 and 12:45 ET quotes of the decision day (ADR 0029).
- **What counts:**
  - live regular-season games from 2026-10-06 on;
  - 2022-23's 342 games, scored once after the freeze (ADR 0025).

  §1's CLV count against Pinnacle's closing proxy starts with those live games.
- **Seen before the freeze, never counted:**
  - The lake holds 2026-27's first 16 regular-season games, played 2026-09-29 to 10-01, with Pinnacle snapshots to 2026-10-02.
  - The guard's `live_moves` was smoke-checked on the 2026-10-01 slate's prices (#142). That read prices only, and no setting changed.
  - No model or policy result was read on these games.
- **A change** to any of these is a new policy version with its own ADR.
  - It is judged only on games after its own freeze date, and the CLV count restarts.
  - `tests/unit/test_freeze.py` fails if a frozen number changes.

## Known weaknesses

B0 is multiplicative-de-vigged, the default since ADR 0008. Power and Shin can't be told apart from it on these seasons: every paired interval includes 0, and the backtest keeps pairing them against it. Its per-season log loss is:
- E1: 0.6726 [0.6565, 0.6878] in 2018-19 and 0.6421 [0.6288, 0.6550] in 2021-22.
- E2: 0.6747 [0.6594, 0.6890] and 0.6463 [0.6337, 0.6596].

Predicting at the opener costs 0.0033 [0.0005, 0.0064] against the close, paired on the games both score.

B1 recalibrates B0's multiplicative probabilities. For the 2018-19 fold it is fitted on 9,370 games in E1 and 9,369 in E2, and for the 2021-22 fold on 12,591 and 12,583. E2 has a few fewer because a few earlier openers are unusable or implausible:
- **Its slope is 1.07 to 1.12** across the four fits, so the market was slightly under-confident over 2010-11 to 2020-21.
- **Its intercept is about -0.03,** a slight overpricing of home teams.
- **It does not beat B0 on these seasons.** B0 minus B1 is +0.0004 [-0.0005, +0.0013] on E1 and +0.0005 [-0.0006, +0.0015] on E2, and no per-season interval excludes 0.

- **SBR's closing vig drops from 3.8% to 2.3% in 2018-19** (audit, #51). The closes likely come from another book from then on, while the openers stay at about 4%. The owner kept the closes as they are (#65). summary.json's `diagnostics.book_era` runs the walk-forward on every season from 2011-12, each B1 fitted before its season, and compares the seasons before the change with those after. It finds no sign that the change hurts:
  - **B1 keeps up with the new book.** B0 minus B1 on E1 is -0.0007 [-0.0014, -0.0000] over 2011-12 to 2017-18 and +0.0002 [-0.0004, +0.0008] over 2018-19 to 2021-22, a difference of +0.0009 [-0.0000, +0.0019]. A B1 fitted mostly on the old book's closes does no worse on the new book's than B1 did within the old book. The old era's figure leans on 2011-12 (-0.0050), whose B1 is fitted on one season.
  - **E2's cost against E1 can't separate the book from timing.** B0's E2 minus E1 is +0.0018 [+0.0007, +0.0029] while one book set both prices and +0.0024 [+0.0007, +0.0042] after, a difference of +0.0005 [-0.0016, +0.0027]. No difference shows, but the interval allows a book effect as large as the whole timing cost.
- **Suspect openers.** `reference/sbr_suspect_openers.csv` (#56) lists 40 openers of 2010-11 to 2021-22 that are likely wrong, among them a swapped side (2018020006), a typo (-1010 in 2021020648) and a 9% price (2018020655). Its `bad_close` column marks 3 of them, all in 2015-16, whose close is the error instead, since the opener and the closing puck line agree against it (#64). B1 still fits on those closes, 3 of the 9,370 games of the 2018-19 fold's fit. Most of its flags read the close, so dropping those games would choose E2's sample with information the opener prediction lacks (hard rule 1).
  - **E2 refuses implausible openers** (ADR 0007): those whose de-vigged home probability is more extreme than every close before their fold, 0.236 to 0.837 for the 2018-19 fold and 0.228 to 0.837 for the 2021-22 fold. The rule reads the opener and earlier closes only, and has nothing tuned. It refuses 10 openers, all from 2018-19 on: #56's 8 extreme ones and two just past the bounds (0.231 and 0.228). 7 scored test games leave. The other 32 listed openers stay in.
  - **E2 on every opener** is reported under `sensitivity` in summary.json: B0 0.6617 [0.6512, 0.6716], and a cost against E1 of +0.0046 [+0.0011, +0.0083].
- **The 2021-22 market is under-confident,** most of all in January 2022, when favourites won 70% of games but were priced at 62%. B1, fitted on the seasons before, corrects only part of it. So a later model's gain concentrated in 2021-22 may still be recalibration.
- **E2 rests on ADR 0006's opener time,** 10:00 US Eastern. From 2018-19 on, all but a handful of openers differ from their close.

**B2 (ADR 0013), from `backtest-20261001-388bb85`:**
- **It is worse than the market,** as a model that reads no price is expected to be. B2 minus B1 is +0.0112 [+0.0044, +0.0185] on E1 and +0.0078 [+0.0013, +0.0146] on E2.
  - Per season on E1: +0.0077 [-0.0011, +0.0180] in 2018-19 and +0.0146 [+0.0050, +0.0245] in 2021-22.
  - On E2: +0.0055 [-0.0028, +0.0154] and +0.0100 [-0.0004, +0.0201].
  - Its own log loss per season is 0.6804 and 0.6558 on E1.
- **It is under-confident.** The calibration slope is 1.35 [1.09, 1.62] on E1, driven by 2021-22 (1.55 [1.25, 1.86]; 2018-19 1.06 [0.59, 1.59]). Its probabilities spread less than the market's (standard deviation 0.082 against 0.122 on E1), though the two agree in direction (correlation 0.78). Expected-goals team strength with an 80-game memory misses talent that shows in goals, and the goalie mixture flattens further.
- **Gaps above 8 points against B1** (hard rule 8): 822 of 2,583 games on E1 (382 in 2018-19, 440 in 2021-22) and 775 of 2,573 on E2. They are listed in `reports/backtest/gaps.csv` without results, and counted in `summary.json`. The largest are games where team strength rates the teams even and the market does not, such as NJD against WSH in March 2019. That many cannot all get a manual review. Gate 1 (#79) decides how they are reviewed, and none led to a change of model.
- **Weights** (on standardized inputs, 2021-22 fold): ΔS 0.29, ΔG 0.06, home back-to-back -0.08, away back-to-back +0.09. Rest, travel, time zones and empty seats are all within 0.03.
- **Lineup quality:** the goalie-start model's Brier score over the team-games of the games B2 scored is 0.415 [0.404, 0.426] on E1 and 0.415 [0.405, 0.427] on E2 (0.405 in 2018-19, 0.424 in 2021-22). 0.9% of starters were not among its candidates.
- **Training on the starters who played** (ADR 0013): Codex read hard rule 9 as forbidding it (P0 on #90). The owner kept it, since the rule governs predictions, and no game's own lineup feeds its own prediction.

**B3 (ADR 0023), from `backtest-20261003-b1a7b04`:**
- **It beats B2.** B3 minus B2 is -0.0079 [-0.0130, -0.0028] on E1 and -0.0078 [-0.0130, -0.0028] on E2.
  - Per season on E1: -0.0066 [-0.0137, +0.0000] in 2018-19 and -0.0091 [-0.0163, -0.0025] in 2021-22.
  - Its own log loss per season is 0.6737 and 0.6467 on E1.
- **It is level with the market at the opener, and short of it at the close.** B3 minus B1 is -0.0000 [-0.0047, +0.0043] on E2 and +0.0033 [-0.0013, +0.0077] on E1.
  - Per season on E1: +0.0010 [-0.0067, +0.0089] and +0.0056 [+0.0004, +0.0106].
  - Being level with B1 is not an edge: the blend (phase 4) is what must add information beyond the market.
- **Gate 2's subsets** (B3 minus B2 on E1; a game is in a subset if either team qualifies):

  | Subset | Games | B3 minus B2 |
  | --- | --- | --- |
  | Trade | 1,652 | -0.0093 [-0.0151, -0.0035] |
  | Injury | 1,811 | -0.0101 [-0.0155, -0.0046] |
  | Lineup change | 39 | -0.0394, no interval |
  | Any | 2,229 | -0.0085 [-0.0134, -0.0035] |

  E2 is within 0.0002 of E1 on each.
  - **The subsets are broad, as defined.** A skater counts as traded for 10 of his games after any move, an offseason signing included. A regular counts as injured when he is missing for any reason.
  - **Every lineup change falls in an opening week.** The projection reads only earlier boxscores, so in season it never drops 3 of the previous game's skaters. The 39 games fall in one week of each season, which a weekly block bootstrap cannot resample, so the report gives them no interval.
  - Gate 2 (ADR 0024) found the lineup-change subset too small to judge in every test, with 20 to 39 games each.
- **It is under-confident, like B2.** The calibration slope is 1.34 [1.11, 1.58] on E1 (2018-19 1.22 [0.85, 1.58], 2021-22 1.42 [1.15, 1.73]), and the intercept is -0.088 [-0.194, +0.020].
- **Gaps above 8 points against B1** (hard rule 8): 411 of 2,583 games on E1 (203 in 2018-19, 208 in 2021-22) and 359 of 2,573 on E2. That is half of B2's. 26 exceed 15 points and 2 exceed 20. They are listed in `reports/backtest/gaps_b3.csv` without results.
  - **Reviewed at gate 2** (`reports/gaps/b3-gap-review.md`, screen `b3-gaps-20261003-291302e`): every flagged game (51), the gap above 20 points, and a seeded sample of 40, read without results. **No data error was found.**
  - **What drives them:** B3 is closer to 50% than the market in 81% of them. Δĝ is the largest term in 313, and a back-to-back or time zone in 98.
  - **The flagged ones show news the boxscores carry late:**
    - summer moves in each season's first weeks (#120);
    - returns from injury and from 2021-22's COVID protocols;
    - rested regulars at season's end;
    - new, recalled or returning goalies.
  - **The sample** is mostly weak teams B3 rates closer to average than the market does: Seattle and Arizona in 2021-22, New Jersey and Anaheim in 2018-19.
- **Weights** (standardized inputs, 2021-22 fold): Δĝ 0.34, home back-to-back -0.07, away back-to-back +0.08. Rest, travel, time zones and empty seats are all within 0.03. The 2018-19 fold gives Δĝ 0.31.
- **Training games:** 8,138 for the 2018-19 fold and 11,359 for the 2021-22 fold, from 2011-12. The three games of 2011-12's opening night have no RAPM league rate, since nothing was public before them, and drop out.
- **Lineup quality on the scored games** (E1):
  - the projection's 5v5 minutes are off by 1.81 [1.78, 1.83] minutes per dressed skater;
  - its power-play unit names 76.1% [75.0%, 77.2%] of the actual top five;
  - the goalie-start Brier score is 0.415 [0.404, 0.426].
- **An expansion team's first game** has no candidate skaters, so it plays its replacements, rated 0. Seattle on 2021-10-12 is the one such game in these seasons. 0 is RAPM's reference skater, not a replacement-level one, so such a team comes out about average (#134).
- **B2's schedule terms, shared with B3, are large.** A home back-to-back costs 0.21 log-odds in the 2021-22 fold. The time-zone terms reach +0.35 in a game played in Europe, which rests on few overseas games in training (#13).
- **The training seasons are in-sample for B3's inputs.** RAPM's, the penalty model's and finishing's settings were tuned on 2010-11 to 2017-18 (ADR 0011). No B3 result on those seasons counts as out-of-sample.
- **Gate 2's hockey validation** (#107; `backtest-hockey-20261003-93d0f92`, `reports/backtest/hockey-20261003-93d0f92.json`). This scores B2 and B3 on the held-out 2023-24 and 2024-25 seasons, on outcomes alone, a second after each game's as-of time. Each fold is trained on every earlier season from 2011-12, 2022-23's results included but none of its prices.

  | | Games | B3 minus B2 |
  | --- | --- | --- |
  | Pooled | 2,624 | -0.0078 [-0.0130, -0.0027] |
  | 2023-24 | 1,312 | -0.0099 [-0.0178, -0.0022] |
  | 2024-25 | 1,312 | -0.0057 [-0.0124, +0.0007] |
  | Trade | 1,723 | -0.0106 [-0.0179, -0.0032] |
  | Injury | 1,650 | -0.0074 [-0.0139, -0.0003] |
  | Lineup change | 37 | -0.0163, no interval (one week in 2023-24) |
  | Any | 2,221 | -0.0084 [-0.0140, -0.0024] |

  - **Log loss:** B3 0.6594 [0.6501, 0.6686] against B2's 0.6672 [0.6587, 0.6759].
  - **B3 is better calibrated here than on the development seasons.** Its slope is 0.94 [0.80, 1.08] (B2's 0.83 [0.70, 0.96]). Its intercept is +0.089 [+0.015, +0.160], so it rates home teams slightly too low, most in 2024-25 (+0.118 [+0.031, +0.209]).
  - **Fits:** Δĝ's weight is 0.39 and 0.40 on standardized inputs. The folds train on 13,983 and 15,295 games, with `train_cutoff` 2023-04-15 and 2024-04-19.
- **Gate 2's one-time 2025-26 test does not confirm B3's gain** (#107; `backtest-hockey-20261003-82fbada`, `reports/backtest/hockey-20261003-82fbada-171522.json`). It ran once, on the owner's go-ahead, after the gap review and the PR's review rounds, in the same hockey-only mode. The run is claimed in R2, beside the lake and in `reports/backtest/one_time_test.txt`, so no machine can run it again.

  | | Games | B3 minus B2 |
  | --- | --- | --- |
  | Pooled | 1,312 | -0.0030 [-0.0094, +0.0034] |
  | Trade | 845 | -0.0003 [-0.0073, +0.0060] |
  | Injury | 847 | -0.0073 [-0.0161, +0.0020] |
  | Lineup change | 20 | +0.0654 [+0.0621, +0.1275] (two weeks only) |
  | Any | 1,114 | -0.0034 [-0.0103, +0.0033] |

  - **Both models are over-confident on 2025-26.** B3's calibration slope is 0.55 [0.29, 0.80] and B2's 0.49 [0.24, 0.77]. Their log loss is 0.6898 [0.6768, 0.7035] and 0.6928 [0.6816, 0.7046], so B2 barely beats a coin flip (0.6931).
  - **The inputs show no data shift** (an inputs-only check, which read no results). The spread of ΔS, ΔG and Δĝ is within the range of 2021-22 to 2024-25. Lineup projections miss as often as before, with 0.33 dressed skaters a team-game unprojected. The actual starter gets the usual probability, 0.58 on average.
  - **Fits:** Δĝ's weight is 0.40 on standardized inputs, trained on 16,607 games with `train_cutoff` 2025-04-18.
  - **The verdict** (ADR 0024): B3 goes forward as the model phase 4 compares with B1, because every test points the same way and 2025-26's interval includes the earlier gain. B3 minus B2 stays in every report (hard rule 3), and the live 2026-27 market test is its next independent evidence.

**The blend and the policy (phase 4, `backtest-20261004-6f74840`; ADR 0026 to 0029):**
- **One season of evidence.** 2018-19 has no earlier out-of-sample fold, so the blend is scored on 2021-22 alone, and the market was itself under-confident that season. 2022-23's 342 games come once, after the freeze (ADR 0025).
- **It beats the opener and not the close.**
  - On E2, BLEND minus B1 is -0.0042 [-0.0078, -0.0007]. It also beats the B2 blend (-0.0033 [-0.0062, -0.0002]) and the market-only control (-0.0049 [-0.0087, -0.0011]), so the gain is not only recalibration.
  - On E1, against the close, it is -0.0015 [-0.0043, +0.0018].
  - The E2 blend's log loss, 0.6410, is about that of the recalibrated close.
- **u adds nothing measurable.** Its weight b_u is +0.22 [-0.06, +0.51] on E2 (from the fit's standard errors), leaning the wrong way: the model is trusted more as doubt grows. In 2021-22, u reaches 5.3 standard deviations among the bets. The policy still reads u in its hurdle and stake. Live u is history's quantity (ADR 0030), so the fit isn't pushed outside its range by confirmations.
- **The bets don't beat the close.**
  - The policy bets 465 of 1,306 games at the opener, mostly away teams and underdogs, at a mean expected return of 7.7%.
  - The return per unit staked is +6.1% [-0.5%, +13.3%].
  - CLV against SBR's close is -1.24% [-2.15%, -0.24%], though the market moved toward the bets: the fair move is +2.82% [+1.90%, +3.85%].
  - The opener's ~4.1% margin costs more than that move: CLV = (1 + fair move) / the opener's overround − 1, and the de-vigged close's own margin plays no part (ADR 0030).
  - Live, the price is Pinnacle's at 12:45 ET, and phase 5 measures CLV against Pinnacle's closing proxy.
- **The data error.** One bet was on a swapped SBR opener (2021020487, `reports/gaps/blend-gap-review.md`). Without it, CLV is -1.53% [-2.25%, -0.79%].
- **Much of the E2 gain is the soft opener.**
  - In 2021-22, B0 lost +0.0049 [-0.0000, +0.0102] of log loss from the close to the opener, against +0.0018 [+0.0007, +0.0029] in 2011-12 to 2017-18.
  - B3 alone is level with that opener: log loss 0.6461 [0.6345, 0.6573] against B0's 0.6463 [0.6337, 0.6596] on the same 1,306 games. Part of the blend's gain is two forecasts of similar quality averaged.
  - Against Pinnacle at 12:45, E1's -0.0015 [-0.0043, +0.0018] is the better guide, which is why live bets use E1's fit (ADR 0030).
- **Bets lean on this season's quirks.**
  - The blend's intercept, -0.08 on E2, tilts every game about 2 points toward the away team, and 77% of the bets are away bets. B3's home bias changes sign between eras: its calibration intercept is -0.089 [-0.194, +0.018] on the development seasons (E2) and +0.089 [+0.015, +0.160] on 2023-24 and 2024-25.
  - All 59 bets on Seattle's games backed Seattle, 13% of the bets, and so did 9 of the 12 with the highest expected return. That is B3's view of the new team (gate 2's review), which no live season repeats.
- **The bet's driver mostly marks favourite against underdog.** `e3.attribution` splits the blend's move from the raw market. Underdog bets come out as "market" (the blend's shrinkage toward 50%) and most favourite bets as "skaters". The driver figures say little about which input carries an edge (#154).
- **The guard can't act on history.** Its θ of 0.0535 is a whole-day move. It would have fired on 7 of the 465 bets between the opener and the close.
- **Gaps above 8 points against B1:** 17 on E1 and 49 on E2, against B3's 208 in 2021-22 on E1. All were reviewed by hand, and no bug was found.

**Phase 4's inputs:**
- **The uncertainty score u (ADR 0026):** history's goalie doubt comes from the goalie-start model alone, since no historical starter confirmations exist. Live, u reads the same goalie-start probabilities and ignores confirmations (ADR 0030), so it stays the quantity the blend was fitted on. B3 itself still uses a confirmed starter.

**Held-out seasons seen for data format only** (#96, PR #108):
- **What was seen:** the first draft of the `penalties` and `faceoffs` tables surveyed every cached season, 2023-24, 2025-26 and the first 2026-27 games included. It recorded which penalty codes exist and which fields can be blank, and it checked the faceoff zones of 200 games of 2023-24.
- **What was not:** no outcome, rate or model figure from those seasons.
- **The fix:** the leakage check caught it. The survey and the zone check were redone on 2010-11 to 2021-22, the design came out the same, and the tables quote only those figures.
- **The owner's ruling, 2026-10-01:** gate 2's tests on those seasons stay valid, and this note records that their format was seen.

## Run history

See `reports/backtest/runs.csv`.
