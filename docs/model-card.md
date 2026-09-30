# Model card

Current production model, how it scored, and its known weaknesses. It is updated only through the `run-backtest` skill, and the accepted metrics live in `reports/backtest/accepted.json`.

## Current model

None. Phase 1 has the two market baselines. B0 is the de-vigged SBR moneyline, with nothing fitted, under the default multiplicative method (ADR 0008). B1 is the recalibrated market, a logistic regression on B0's log-odds fitted per test season on the earlier seasons. Backtest `backtest-20260930-7301709` on the development seasons 2018-19 and 2021-22. E2 refuses implausible SBR openers (ADR 0007).

## Metrics

Every metric is reported with a 95% weekly block bootstrap interval, pooled over test seasons and per season.

| Metric | B0 | B1 | B2 | B3 | Blend |
| --- | --- | --- | --- | --- | --- |
| Log loss, E1 (close) | 0.6571 [0.6469, 0.6668] | 0.6567 [0.6456, 0.6670] | | | |
| Log loss, E2 (opener) | 0.6603 [0.6505, 0.6698] | 0.6598 [0.6491, 0.6702] | | | |
| Paired log-loss difference against B1, E1 | +0.0004 [-0.0005, +0.0013] | reference | | | |
| Paired log-loss difference against B1, E2 | +0.0005 [-0.0006, +0.0015] | reference | | | |
| Calibration intercept | | | | | |
| Calibration slope | | | | | |
| E3 CLV under the frozen policy | | | | | |

## Artifact versions

| Component | Version | train_cutoff |
| --- | --- | --- |
| Backtest (B0, B1; E2 refuses implausible openers, with every opener as a sensitivity) | backtest-20260930-7301709 | per fold, below |
| B0 | the de-vigged market (multiplicative, ADR 0008) | none: B0 fits nothing |
| B1, 2018-19 fold (E1 and E2) | backtest-20260930-7301709 | 2018-04-09 10:00 UTC |
| B1, 2021-22 fold (E1 and E2) | backtest-20260930-7301709 | 2021-05-20 10:00 UTC |

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

## Run history

See `reports/backtest/runs.csv`.
