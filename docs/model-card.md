# Model card

Current production model, how it scored, and its known weaknesses. It is updated only through the `run-backtest` skill, and the accepted metrics live in `reports/backtest/accepted.json`.

## Current model

None. The backtest has the two market baselines and phase 2's model:
- **B0** is the de-vigged SBR moneyline, with nothing fitted, under the default multiplicative method (ADR 0008).
- **B1** is the recalibrated market, a logistic regression on B0's log-odds fitted per test season on the earlier seasons.
- **B2** is the team and goalie model (ADR 0013). It is an L2 logistic regression on team strength, goalie effects, rest and travel and empty seats, with the season home edge as a fixed term. Each fold fits it on earlier games with their actual starters. It predicts by averaging over the goalie-start model's pairs of starters, and reads no price. Gate 1 (#79) judges it against B1.

The run is backtest `backtest-20261001-388bb85` on the development seasons 2018-19 and 2021-22. E2 refuses implausible SBR openers (ADR 0007). B0 and B1 are unchanged from `backtest-20260930-7301709`.

## Metrics

Every metric is reported with a 95% weekly block bootstrap interval, pooled over test seasons and per season.

| Metric | B0 | B1 | B2 | B3 | Blend |
| --- | --- | --- | --- | --- | --- |
| Log loss, E1 (close) | 0.6571 [0.6469, 0.6668] | 0.6567 [0.6456, 0.6670] | 0.6679 [0.6607, 0.6749] | | |
| Log loss, E2 (opener) | 0.6603 [0.6505, 0.6698] | 0.6598 [0.6491, 0.6702] | 0.6677 [0.6605, 0.6748] | | |
| Paired log-loss difference against B1, E1 | +0.0004 [-0.0005, +0.0013] | reference | +0.0112 [+0.0044, +0.0185] | | |
| Paired log-loss difference against B1, E2 | +0.0005 [-0.0006, +0.0015] | reference | +0.0078 [+0.0013, +0.0146] | | |
| Calibration intercept | | | E1 -0.098 [-0.211, +0.015]; E2 -0.099 [-0.209, +0.013] | | |
| Calibration slope | | | E1 1.35 [1.09, 1.62]; E2 1.35 [1.10, 1.62] | | |
| E3 CLV under the frozen policy | | | | | |

## Artifact versions

| Component | Version | train_cutoff |
| --- | --- | --- |
| Backtest (B0, B1, B2; E2 refuses implausible openers, with every opener as a sensitivity) | backtest-20261001-388bb85 | per fold, below |
| B0 | the de-vigged market (multiplicative, ADR 0008) | none: B0 fits nothing |
| B1, 2018-19 fold (E1 and E2) | backtest-20261001-388bb85 | 2018-04-09 10:00 UTC |
| B1, 2021-22 fold (E1 and E2) | backtest-20261001-388bb85 | 2021-05-20 10:00 UTC |
| B2, 2018-19 fold (E1 and E2), L2 100 tuned by b2-20261001-fe11def | backtest-20261001-388bb85 | 2018-04-09 10:00 UTC |
| B2, 2021-22 fold (E1 and E2) | backtest-20261001-388bb85 | 2021-05-20 10:00 UTC |
| Team strength ΔS (ADR 0011) | team-strength-20261001-1b2a5b8 | 2018-04-09 10:00 UTC (tuning) |
| Goalie-start model (ADR 0012) | goalie-start-20261001-784c4fb | per season, before its first game |
| Goalie effects ΔG (ADR 0011) | goalie-effect-20261001-c05c300 | 2018-04-09 10:00 UTC (tuning) |
| Schedule terms and home edge h_s (ADR 0011) | schedule-terms-20261001-84bc182 | 2018-04-09 10:00 UTC (tuning) |
| xG (ADR 0010) | xg-20261001-de27a2c | per season, before its first game |

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

**Held-out seasons seen for data format only** (#96, PR #108):
- **What was seen:** the first draft of the `penalties` and `faceoffs` tables surveyed every cached season, 2023-24, 2025-26 and the first 2026-27 games included. It recorded which penalty codes exist and which fields can be blank, and it checked the faceoff zones of 200 games of 2023-24.
- **What was not:** no outcome, rate or model figure from those seasons.
- **The fix:** the leakage check caught it. The survey and the zone check were redone on 2010-11 to 2021-22, the design came out the same, and the tables quote only those figures.
- **The owner's ruling, 2026-10-01:** gate 2's tests on those seasons stay valid, and this note records that their format was seen.

## Run history

See `reports/backtest/runs.csv`.
