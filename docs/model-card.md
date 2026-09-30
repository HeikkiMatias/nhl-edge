# Model card

Current production model, how it scored, and its known weaknesses. It is updated only through the `run-backtest` skill, and the accepted metrics live in `reports/backtest/accepted.json`.

## Current model

None. Phase 1 has the two market baselines. B0 is the de-vigged SBR moneyline, with nothing fitted. B1 is the recalibrated market, a logistic regression on B0's log-odds fitted per test season on the earlier seasons. Backtest `backtest-20260930-9911773` on the development seasons 2018-19 and 2021-22.

## Metrics

Every metric is reported with a 95% weekly block bootstrap interval, pooled over test seasons and per season.

| Metric | B0 | B1 | B2 | B3 | Blend |
| --- | --- | --- | --- | --- | --- |
| Log loss, E1 (close) | 0.6571 [0.6469, 0.6668] | 0.6567 [0.6456, 0.6670] | | | |
| Log loss, E2 (opener) | 0.6617 [0.6512, 0.6716] | 0.6615 [0.6503, 0.6722] | | | |
| Paired log-loss difference against B1, E1 | +0.0004 [-0.0005, +0.0013] | reference | | | |
| Paired log-loss difference against B1, E2 | +0.0002 [-0.0008, +0.0012] | reference | | | |
| Calibration intercept | | | | | |
| Calibration slope | | | | | |
| E3 CLV under the frozen policy | | | | | |

## Artifact versions

| Component | Version | train_cutoff |
| --- | --- | --- |
| Backtest (B0, B1, E2 without the suspect openers) | backtest-20260930-9911773 | per fold, below |
| B0 | the de-vigged market (multiplicative) | none: B0 fits nothing |
| B1, 2018-19 fold (E1 and E2) | backtest-20260930-9911773 | 2018-04-09 10:00 UTC |
| B1, 2021-22 fold (E1 and E2) | backtest-20260930-9911773 | 2021-05-20 10:00 UTC |

## Known weaknesses

B0 is multiplicative-de-vigged. Power and Shin can't be told apart from it on these seasons: every paired interval includes 0. Its per-season log loss is:
- E1: 0.6726 [0.6565, 0.6878] in 2018-19 and 0.6421 [0.6288, 0.6550] in 2021-22.
- E2: 0.6747 [0.6587, 0.6899] and 0.6491 [0.6359, 0.6627].

Predicting at the opener costs 0.0046 [0.0011, 0.0083] against the close, paired on the games both score.

B1 recalibrates B0's multiplicative probabilities. For the 2018-19 fold it is fitted on 9,370 games in E1 and 9,369 in E2, and for the 2021-22 fold on 12,591 and 12,589. E2 has a few fewer because a few earlier games have no usable opener:
- **Its slope is 1.07 to 1.12** across the four fits, so the market was slightly under-confident over 2010-11 to 2020-21.
- **Its intercept is about -0.03,** a slight overpricing of home teams.
- **It does not beat B0 on these seasons.** B0 minus B1 is +0.0004 [-0.0005, +0.0013] on E1 and +0.0002 [-0.0008, +0.0012] on E2, and no per-season interval excludes 0.

- **SBR's closing vig drops from 3.8% to 2.3% in 2018-19** (audit, #51). The closes likely come from another book from then on, while the openers stay at about 4%. So E2 against E1 mixes timing with a change of book.
- **Suspect openers.** `reference/sbr_suspect_openers.csv` (#56) lists 40 openers of 2010-11 to 2021-22 that are likely wrong, among them a swapped side (2018020006), a typo (-1010 in 2021020648) and a 9% price (2018020655). E2 keeps them, and B1's E2 fits read them, until the owner decides. The backtest reports E2 without them beside it, under `sensitivity` in summary.json. The list reads the close, so these figures are hindsight: they size the data problem and are not a tradable result.
  - **Without all 40,** 8 scored test games leave E2 (3 more were already refused). E2's cost against E1 drops from +0.0046 [+0.0011, +0.0083] to +0.0026 [+0.0000, +0.0054] for B0.
  - **Without only the 26 proven errors** (extreme_open, swapped or below_100), 6 scored test games leave, and the cost is +0.0032 [+0.0004, +0.0062].
- **The 2021-22 market is under-confident,** most of all in January 2022, when favourites won 70% of games but were priced at 62%. B1, fitted on the seasons before, corrects only part of it. So a later model's gain concentrated in 2021-22 may still be recalibration.
- **E2 rests on ADR 0006's opener time,** 10:00 US Eastern. From 2018-19 on, all but a handful of openers differ from their close.

## Run history

See `reports/backtest/runs.csv`.
