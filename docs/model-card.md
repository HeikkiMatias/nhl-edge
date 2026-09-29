# Model card

Current production model, how it scored, and its known weaknesses. It is updated only through the `run-backtest` skill, and the accepted metrics live in `reports/backtest/accepted.json`.

## Current model

None. Phase 1 has the market baseline B0 only: the de-vigged SBR moneyline, with nothing fitted. Backtest `backtest-20260929-12d2957` on the development seasons 2018-19 and 2021-22.

## Metrics

Every metric is reported with a 95% weekly block bootstrap interval, pooled over test seasons and per season.

| Metric | B0 | B1 | B2 | B3 | Blend |
| --- | --- | --- | --- | --- | --- |
| Log loss, E1 (close) | 0.6571 [0.6469, 0.6668] | | | | |
| Log loss, E2 (opener) | 0.6617 [0.6512, 0.6716] | | | | |
| Paired log-loss difference against B1 | | | | | |
| Calibration intercept | | | | | |
| Calibration slope | | | | | |
| E3 CLV under the frozen policy | | | | | |

## Artifact versions

| Component | Version | train_cutoff |
| --- | --- | --- |
| Backtest (B0) | backtest-20260929-12d2957 | none: B0 fits nothing |

## Known weaknesses

B0 is multiplicative-de-vigged. Power and Shin can't be told apart from it on these seasons: every paired interval includes 0. Its per-season log loss is:
- E1: 0.6726 [0.6565, 0.6878] in 2018-19 and 0.6421 [0.6288, 0.6550] in 2021-22.
- E2: 0.6747 [0.6587, 0.6899] and 0.6491 [0.6359, 0.6627].

Predicting at the opener costs 0.0046 [0.0011, 0.0083] against the close, paired on the games both score.

- **SBR's closing vig drops from 3.8% to 2.3% in 2018-19** (audit, #51). The closes likely come from another book from then on, while the openers stay at about 4%. So E2 against E1 mixes timing with a change of book.
- **Suspect openers.** A few development-season openers look wrong: a swapped side (2018020006), a typo (-1010 in 2021020648), and a 9% price (2018020655). They weigh heavily on E2 until they are reviewed.
- **The 2021-22 market is under-confident,** most of all in January 2022, when favourites won 70% of games but were priced at 62%. A later model's gain concentrated in 2021-22 may only be recalibration, which B1 corrects.
- **E2 rests on ADR 0006's opener time,** 10:00 US Eastern. From 2018-19 on, all but a handful of openers differ from their close.

## Run history

See `reports/backtest/runs.csv`.
